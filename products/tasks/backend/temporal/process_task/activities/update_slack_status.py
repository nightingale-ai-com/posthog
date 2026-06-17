"""Short Temporal activities that drive a Slack ``chat.startStream`` lifecycle.

The per-turn :class:`SlackStatusRelayWorkflow` dispatches these as a sequence:

* :func:`start_slack_status_stream` — opens the stream with the first
  ``task_update`` chunk and returns the message ``ts`` for the rest of the turn.
* :func:`append_slack_status_step` — transitions the previous step to
  ``complete`` and the next one to ``in_progress`` in a single ``appendStream``.
  Optionally appends a ``markdown_text`` chunk in the same call to stream the
  agent's narrative under the plan block.
* :func:`stop_slack_status_stream` — marks the trailing step complete and closes
  the stream.

Failures are logged and swallowed; a transient Slack outage must never
escalate to a task failure.
"""

from dataclasses import dataclass
from typing import Any, Optional

from temporalio import activity

from posthog.temporal.common.logger import get_logger
from posthog.temporal.common.utils import close_db_connections

logger = get_logger(__name__)


@dataclass
class StartSlackStatusStreamInput:
    slack_thread_context: dict[str, Any]
    first_task_id: str
    first_task_title: str
    first_task_details: Optional[str] = None


@dataclass
class AppendSlackStatusStepInput:
    slack_thread_context: dict[str, Any]
    ts: str
    complete_task_id: Optional[str]
    complete_task_title: Optional[str]
    complete_task_details: Optional[str]
    new_task_id: Optional[str]
    new_task_title: Optional[str]
    new_task_details: Optional[str]
    markdown_text: Optional[str] = None


@dataclass
class StopSlackStatusStreamInput:
    slack_thread_context: dict[str, Any]
    ts: str
    complete_task_id: Optional[str] = None
    complete_task_title: Optional[str] = None
    complete_task_details: Optional[str] = None


@activity.defn
@close_db_connections
def start_slack_status_stream(input: StartSlackStatusStreamInput) -> Optional[str]:
    """Open a streaming status message and return its ``ts``.

    Returns ``None`` when the stream cannot be opened (missing recipient, Slack
    error). The workflow treats that as "skip streaming for this turn".
    """
    from products.slack_app.backend.slack_thread import SlackThreadContext, SlackThreadHandler

    try:
        context = SlackThreadContext.from_dict(input.slack_thread_context)
        return SlackThreadHandler(context).start_status_stream(
            first_task_id=input.first_task_id,
            first_task_title=input.first_task_title,
            first_task_details=input.first_task_details,
        )
    except Exception as e:
        logger.warning("start_slack_status_stream_failed", error=str(e))
        return None


@activity.defn
@close_db_connections
def append_slack_status_step(input: AppendSlackStatusStepInput) -> None:
    from products.slack_app.backend.slack_thread import SlackThreadContext, SlackThreadHandler

    try:
        context = SlackThreadContext.from_dict(input.slack_thread_context)
        SlackThreadHandler(context).append_status_step(
            ts=input.ts,
            complete_task_id=input.complete_task_id,
            complete_task_title=input.complete_task_title,
            complete_task_details=input.complete_task_details,
            new_task_id=input.new_task_id,
            new_task_title=input.new_task_title,
            new_task_details=input.new_task_details,
            markdown_text=input.markdown_text,
        )
    except Exception as e:
        logger.warning("append_slack_status_step_failed", error=str(e))


@activity.defn
@close_db_connections
def stop_slack_status_stream(input: StopSlackStatusStreamInput) -> None:
    from products.slack_app.backend.slack_thread import SlackThreadContext, SlackThreadHandler

    try:
        context = SlackThreadContext.from_dict(input.slack_thread_context)
        SlackThreadHandler(context).stop_status_stream(
            ts=input.ts,
            complete_task_id=input.complete_task_id,
            complete_task_title=input.complete_task_title,
            complete_task_details=input.complete_task_details,
        )
    except Exception as e:
        logger.warning("stop_slack_status_stream_failed", error=str(e))
