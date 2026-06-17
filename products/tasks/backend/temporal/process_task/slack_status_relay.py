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
        self._pending_step: Optional[PendingStep] = None
        self._pending_markdown_buffer: str = ""
        self._stream_ts: Optional[str] = None
        self._current_task_id: Optional[str] = None
        self._current_task_title: Optional[str] = None
        self._current_task_details: Optional[str] = None
        self._last_dispatched_at: float = 0.0
        self._turn_complete: bool = False

    @workflow.signal
    async def agent_status_update(self, payload: dict[str, Any]) -> None:
        """Queue a plan-block step transition.

        ``payload``: ``{"title": str, "details": str | None}``. Accepts the old
        ``str`` shape too for compatibility with in-flight relays that emit
        the pre-enrichment payload.
        """
        if isinstance(payload, str):
            self._pending_step = PendingStep(title=payload, details=None)
            return
        title = payload.get("title") or payload.get("text")
        if not isinstance(title, str) or not title:
            return
        details = payload.get("details")
        self._pending_step = PendingStep(
            title=title,
            details=details if isinstance(details, str) and details else None,
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
        return self._pending_step is not None or bool(self._pending_markdown_buffer)

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

                step = self._pending_step
                self._pending_step = None
                markdown = self._pending_markdown_buffer
                self._pending_markdown_buffer = ""

                step_changed = step is not None and (
                    step.title != self._current_task_title or step.details != self._current_task_details
                )
                if not step_changed and not markdown:
                    continue

                self._last_dispatched_at = workflow.now().timestamp()

                if self._stream_ts is None:
                    # First flush of the turn — must include a step to seed the
                    # plan block. If we only have markdown so far, drop it and
                    # wait; the agent will produce a tool step shortly.
                    if step is None:
                        self._pending_markdown_buffer = markdown + self._pending_markdown_buffer
                        continue
                    first_id = str(workflow.uuid4())
                    self._stream_ts = await workflow.execute_activity(
                        start_slack_status_stream,
                        StartSlackStatusStreamInput(
                            slack_thread_context=input.slack_thread_context,
                            first_task_id=first_id,
                            first_task_title=step.title,
                            first_task_details=step.details,
                        ),
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    if self._stream_ts is None:
                        # Slack rejected the stream open — exit this turn.
                        return
                    self._current_task_id = first_id
                    self._current_task_title = step.title
                    self._current_task_details = step.details
                    if markdown:
                        # Stream the buffered narrative right after seeding.
                        await workflow.execute_activity(
                            append_slack_status_step,
                            AppendSlackStatusStepInput(
                                slack_thread_context=input.slack_thread_context,
                                ts=self._stream_ts,
                                complete_task_id=None,
                                complete_task_title=None,
                                complete_task_details=None,
                                new_task_id=None,
                                new_task_title=None,
                                new_task_details=None,
                                markdown_text=markdown,
                            ),
                            start_to_close_timeout=timedelta(seconds=10),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    continue

                # Subsequent flush — single appendStream covers step transition
                # (if any) and narrative text (if any).
                if step_changed and step is not None:
                    new_id = str(workflow.uuid4())
                    new_title: Optional[str] = step.title
                    new_details: Optional[str] = step.details
                else:
                    new_id = None
                    new_title = None
                    new_details = None
                await workflow.execute_activity(
                    append_slack_status_step,
                    AppendSlackStatusStepInput(
                        slack_thread_context=input.slack_thread_context,
                        ts=self._stream_ts,
                        complete_task_id=self._current_task_id if step_changed else None,
                        complete_task_title=self._current_task_title if step_changed else None,
                        complete_task_details=self._current_task_details if step_changed else None,
                        new_task_id=new_id,
                        new_task_title=new_title,
                        new_task_details=new_details,
                        markdown_text=markdown or None,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                if step_changed and step is not None:
                    self._current_task_id = new_id
                    self._current_task_title = step.title
                    self._current_task_details = step.details
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
