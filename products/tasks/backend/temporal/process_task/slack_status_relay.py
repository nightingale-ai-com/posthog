"""Per-turn child workflow that streams live agent-status updates to Slack.

Spawned once per agent turn by ``ProcessTaskWorkflow``. Receives
``agent_status_update`` signals (forwarded from the parent's own signal handler,
which itself receives them from the ``relay_sandbox_events`` activity), and
dispatches a short ``update_slack_status`` activity per coalesced status change.

Why a separate per-turn workflow rather than a long-running activity:
- atomic turn lifecycle — state never leaks across turns
- deterministic debounce / throttle math, time-skipping testable
- no new long-running activity to worry about (heartbeats, cancellation)

Rate-limit shape:
- debounce 1 s so bursts collapse into one update with the latest text
- throttle ≥ 2 s between dispatches to stay under Slack's tier-3 ceiling
- one in-flight ``update_slack_status`` activity at a time

Safety net (in case ``complete_turn`` is lost — activity crash, worker restart):
- ``TURN_IDLE_TIMEOUT_MINUTES`` (5 min) of no signals exits gracefully
- ``execution_timeout`` on the child handle is a hard ceiling (set by parent)
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow

with workflow.unsafe.imports_passed_through():
    from .activities.update_slack_status import UpdateSlackStatusInput, update_slack_status


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
        self._last_dispatched_text: Optional[str] = None
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
        while not self._turn_complete:
            try:
                await workflow.wait_condition(
                    lambda: self._pending_text is not None or self._turn_complete,
                    timeout=timedelta(minutes=TURN_IDLE_TIMEOUT_MINUTES),
                )
            except TimeoutError:
                # turn_completed never arrived — fall out so Temporal can free
                # the run. The parent has its own inactivity timeout for the
                # task as a whole; a stranded relay shouldn't outlive it.
                workflow.logger.warning(
                    "slack_status_relay_idle_timeout",
                    extra={"workflow_id": workflow.info().workflow_id},
                )
                return

            if self._turn_complete:
                break

            # Debounce: wait for the burst to settle, then take the latest.
            await workflow.sleep(STATUS_DEBOUNCE_SECONDS)

            # Throttle: never dispatch within STATUS_MIN_INTERVAL_SECONDS of the
            # previous dispatch start. Keeps us comfortably under Slack's
            # tier-3 chat.update ceiling (~50/min).
            elapsed = workflow.now().timestamp() - self._last_dispatched_at
            if elapsed < STATUS_MIN_INTERVAL_SECONDS:
                await workflow.sleep(STATUS_MIN_INTERVAL_SECONDS - elapsed)

            text = self._pending_text
            self._pending_text = None

            if text is None or text == self._last_dispatched_text:
                continue

            self._last_dispatched_text = text
            self._last_dispatched_at = workflow.now().timestamp()

            await workflow.execute_activity(
                update_slack_status,
                UpdateSlackStatusInput(
                    slack_thread_context=input.slack_thread_context,
                    text=text,
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
