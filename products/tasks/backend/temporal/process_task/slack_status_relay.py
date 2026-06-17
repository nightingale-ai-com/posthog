"""Per-turn child workflow that streams live agent-status updates to Slack.

Spawned once per agent turn by :class:`ProcessTaskWorkflow`. Opens a Slack
streaming response via ``chat.startStream`` with ``task_display_mode='plan'`` —
rendering as the native collapsible plan block in the channel thread — and
appends ``task_update`` chunks as the agent moves between actions plus
``markdown_text`` chunks for the agent's narrative body text.

Two kinds of signals drive the stream:

* :meth:`agent_status_update` — a tool started or transitioned. Buffers the
  latest pending step (title + details). On flush, marks the previous step
  ``complete`` and the new step ``in_progress``.
* :meth:`agent_text_delta` — a slice of assistant narrative text. Buffered
  and emitted as a ``markdown_text`` chunk on the same flush tick.

Both buffers drain through one debounced + throttled flusher so a single
``chat.appendStream`` call covers any combination of step transition + text.

Why a separate per-turn workflow rather than a long-running activity:

* atomic turn lifecycle — state never leaks across turns
* deterministic debounce / throttle math, time-skipping testable
* no new long-running activity to worry about (heartbeats, cancellation)

Rate-limit shape:

* debounce 1 s so bursts collapse into one append with the latest text
* throttle ≥ 2 s between dispatches
* one in-flight ``append_slack_status_step`` activity at a time

Safety net (in case ``complete_turn`` is lost — activity crash, worker restart):

* :data:`TURN_IDLE_TIMEOUT_MINUTES` (5 min) of no signals exits gracefully
* ``execution_timeout`` on the child handle is a hard ceiling (set by parent)
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow

with workflow.unsafe.imports_passed_through():
    from .activities.update_slack_status import (
        AppendSlackStatusStepInput,
        StartSlackStatusStreamInput,
        StopSlackStatusStreamInput,
        TaskUpdateChunk,
        append_slack_status_step,
        start_slack_status_stream,
        stop_slack_status_stream,
    )


STATUS_DEBOUNCE_SECONDS = 1.0
STATUS_MIN_INTERVAL_SECONDS = 2.0
TURN_IDLE_TIMEOUT_MINUTES = 5


@dataclass
class PendingStep:
    """A queued plan-block step waiting to be flushed to Slack."""

    title: str
    details: Optional[str]


@dataclass
class SlackStatusRelayInput:
    slack_thread_context: dict[str, Any]


@workflow.defn(name="slack-status-relay")
class SlackStatusRelayWorkflow(PostHogWorkflow):
    def __init__(self) -> None:
        # Queue of distinct steps observed since the last flush. Multiple
        # identical-name tool calls each enqueue a separate entry so the
        # plan block shows them as separate transitions (each with its own
        # generated id) rather than collapsing into one.
        self._pending_steps: list[PendingStep] = []
        self._pending_markdown_buffer: str = ""
        self._stream_ts: Optional[str] = None
        self._current_task_id: Optional[str] = None
        self._current_task_title: Optional[str] = None
        self._current_task_details: Optional[str] = None
        self._last_dispatched_at: float = 0.0
        self._turn_complete: bool = False

    @workflow.signal
    async def agent_status_update(self, payload: dict[str, Any]) -> None:
        """Enqueue a plan-block step transition.

        Each ``_posthog/status`` notification is a distinct tool start (the
        agent emits one per ``!alreadyCached`` tool_use), so every signal
        becomes a separate step — we never deduplicate by title/details.

        ``payload``: ``{"title": str, "details": str | None}``. Accepts the
        old ``str`` shape too for compatibility with in-flight relays that
        emit the pre-enrichment payload.
        """
        if isinstance(payload, str):
            self._pending_steps.append(PendingStep(title=payload, details=None))
            return
        title = payload.get("title") or payload.get("text")
        if not isinstance(title, str) or not title:
            return
        details = payload.get("details")
        self._pending_steps.append(
            PendingStep(
                title=title,
                details=details if isinstance(details, str) and details else None,
            )
        )

    @workflow.signal
    async def agent_text_delta(self, text: str) -> None:
        """Append a slice of agent narrative text to the pending markdown buffer."""
        if not isinstance(text, str) or not text:
            return
        self._pending_markdown_buffer += text

    @workflow.signal
    async def complete_turn(self) -> None:
        self._turn_complete = True

    def _has_pending(self) -> bool:
        return bool(self._pending_steps) or bool(self._pending_markdown_buffer)

    def _build_transition_chunks(self, steps: list[PendingStep]) -> list[TaskUpdateChunk]:
        """Build the ordered transition chunks for one flush.

        Pattern (in order in the resulting plan block):

        1. The previously-active step → ``complete`` (if there was one).
        2. Every intermediate queued step → ``complete`` (each is a tool
           that started and whose successor has already started, so by the
           time we flush they're definitionally finished).
        3. The last queued step → ``in_progress`` (becomes the new current).

        Also mutates ``self._current_*`` to point at the new in_progress step.
        """
        chunks: list[TaskUpdateChunk] = []
        if not steps:
            return chunks
        if self._current_task_id and self._current_task_title:
            chunks.append(
                TaskUpdateChunk(
                    id=self._current_task_id,
                    title=self._current_task_title,
                    status="complete",
                    details=self._current_task_details,
                )
            )
        for s in steps[:-1]:
            chunks.append(
                TaskUpdateChunk(
                    id=str(workflow.uuid4()),
                    title=s.title,
                    status="complete",
                    details=s.details,
                )
            )
        last = steps[-1]
        last_id = str(workflow.uuid4())
        chunks.append(
            TaskUpdateChunk(
                id=last_id,
                title=last.title,
                status="in_progress",
                details=last.details,
            )
        )
        self._current_task_id = last_id
        self._current_task_title = last.title
        self._current_task_details = last.details
        return chunks

    @workflow.run
    async def run(self, input: SlackStatusRelayInput) -> None:
        try:
            while not self._turn_complete:
                try:
                    await workflow.wait_condition(
                        lambda: self._has_pending() or self._turn_complete,
                        timeout=timedelta(minutes=TURN_IDLE_TIMEOUT_MINUTES),
                    )
                except TimeoutError:
                    workflow.logger.warning(
                        "slack_status_relay_idle_timeout",
                        extra={"workflow_id": workflow.info().workflow_id},
                    )
                    return

                if self._turn_complete:
                    break

                await workflow.sleep(STATUS_DEBOUNCE_SECONDS)

                elapsed = workflow.now().timestamp() - self._last_dispatched_at
                if elapsed < STATUS_MIN_INTERVAL_SECONDS:
                    await workflow.sleep(STATUS_MIN_INTERVAL_SECONDS - elapsed)

                steps = self._pending_steps
                self._pending_steps = []
                markdown = self._pending_markdown_buffer
                self._pending_markdown_buffer = ""

                if not steps and not markdown:
                    continue

                self._last_dispatched_at = workflow.now().timestamp()

                if self._stream_ts is None:
                    # First flush — must include at least one step to seed
                    # the plan block. If only narrative is pending (the agent
                    # isn't emitting ``_posthog/status``, e.g. older sandbox
                    # ``@posthog/agent`` predating PR A), synthesize a generic
                    # ``Thinking`` step so the body can still stream.
                    if not steps:
                        steps = [PendingStep(title="Thinking", details=None)]
                    first_step = steps[0]
                    first_id = str(workflow.uuid4())
                    self._stream_ts = await workflow.execute_activity(
                        start_slack_status_stream,
                        StartSlackStatusStreamInput(
                            slack_thread_context=input.slack_thread_context,
                            first_task_id=first_id,
                            first_task_title=first_step.title,
                            first_task_details=first_step.details,
                        ),
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    if self._stream_ts is None:
                        # Slack rejected the stream open — exit this turn.
                        return
                    self._current_task_id = first_id
                    self._current_task_title = first_step.title
                    self._current_task_details = first_step.details
                    remaining = steps[1:]
                    if remaining or markdown:
                        await workflow.execute_activity(
                            append_slack_status_step,
                            AppendSlackStatusStepInput(
                                slack_thread_context=input.slack_thread_context,
                                ts=self._stream_ts,
                                task_updates=self._build_transition_chunks(remaining),
                                markdown_text=markdown or None,
                            ),
                            start_to_close_timeout=timedelta(seconds=10),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    continue

                # Subsequent flush — single appendStream covers any number of
                # step transitions plus the narrative text.
                await workflow.execute_activity(
                    append_slack_status_step,
                    AppendSlackStatusStepInput(
                        slack_thread_context=input.slack_thread_context,
                        ts=self._stream_ts,
                        task_updates=self._build_transition_chunks(steps),
                        markdown_text=markdown or None,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
        finally:
            if self._stream_ts is not None:
                await workflow.execute_activity(
                    stop_slack_status_stream,
                    StopSlackStatusStreamInput(
                        slack_thread_context=input.slack_thread_context,
                        ts=self._stream_ts,
                        complete_task_id=self._current_task_id,
                        complete_task_title=self._current_task_title,
                        complete_task_details=self._current_task_details,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
