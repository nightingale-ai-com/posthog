"""Per-turn child workflow that streams live agent-status updates to Slack.

Spawned once per agent turn by :class:`ProcessTaskWorkflow`. Opens a Slack
streaming response via ``chat.startStream`` with ``task_display_mode='plan'`` —
rendering as the native collapsible plan block in the channel thread — and
appends ``task_update`` chunks as the agent moves between actions. Each new
status from :mod:`relay_sandbox_events` marks the previous step ``complete`` and
the new step ``in_progress``; ``complete_turn`` finalizes the last step and
closes the stream.

Why a separate per-turn workflow rather than a long-running activity:

* atomic turn lifecycle — state never leaks across turns
* deterministic debounce / throttle math, time-skipping testable
* no new long-running activity to worry about (heartbeats, cancellation)

Rate-limit shape (Slack's streaming methods share the same tier-3 ceilings as
``chat.update``):

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
class SlackStatusRelayInput:
    slack_thread_context: dict[str, Any]


@workflow.defn(name="slack-status-relay")
class SlackStatusRelayWorkflow(PostHogWorkflow):
    def __init__(self) -> None:
        self._pending_text: Optional[str] = None
        self._stream_ts: Optional[str] = None
        self._current_task_id: Optional[str] = None
        self._current_task_title: Optional[str] = None
        self._last_dispatched_at: float = 0.0
        self._turn_complete: bool = False

    @workflow.signal
    async def agent_status_update(self, text: str) -> None:
        self._pending_text = text

    @workflow.signal
    async def complete_turn(self) -> None:
        self._turn_complete = True

    @workflow.run
    async def run(self, input: SlackStatusRelayInput) -> None:
        try:
            while not self._turn_complete:
                try:
                    await workflow.wait_condition(
                        lambda: self._pending_text is not None or self._turn_complete,
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

                text = self._pending_text
                self._pending_text = None

                if text is None or text == self._current_task_title:
                    continue

                self._last_dispatched_at = workflow.now().timestamp()

                if self._stream_ts is None:
                    # First step of the turn — open the stream.
                    first_id = str(workflow.uuid4())
                    self._stream_ts = await workflow.execute_activity(
                        start_slack_status_stream,
                        StartSlackStatusStreamInput(
                            slack_thread_context=input.slack_thread_context,
                            first_task_id=first_id,
                            first_task_title=text,
                        ),
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                    if self._stream_ts is None:
                        # Slack rejected the stream open (missing scope, missing
                        # recipient, transient outage) — there's nothing further
                        # to send for this turn. Exit so the parent doesn't
                        # spawn endless retries; legacy placeholder path stays
                        # quiet because the gate already skipped it.
                        return
                    self._current_task_id = first_id
                    self._current_task_title = text
                    continue

                # Subsequent step — complete the previous, start a new one.
                new_id = str(workflow.uuid4())
                await workflow.execute_activity(
                    append_slack_status_step,
                    AppendSlackStatusStepInput(
                        slack_thread_context=input.slack_thread_context,
                        ts=self._stream_ts,
                        complete_task_id=self._current_task_id,
                        complete_task_title=self._current_task_title,
                        new_task_id=new_id,
                        new_task_title=text,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                self._current_task_id = new_id
                self._current_task_title = text
        finally:
            if self._stream_ts is not None:
                await workflow.execute_activity(
                    stop_slack_status_stream,
                    StopSlackStatusStreamInput(
                        slack_thread_context=input.slack_thread_context,
                        ts=self._stream_ts,
                        complete_task_id=self._current_task_id,
                        complete_task_title=self._current_task_title,
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
