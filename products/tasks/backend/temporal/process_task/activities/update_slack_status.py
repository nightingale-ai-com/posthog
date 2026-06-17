from dataclasses import dataclass
from typing import Any

from temporalio import activity

from posthog.temporal.common.logger import get_logger
from posthog.temporal.common.utils import close_db_connections

logger = get_logger(__name__)


@dataclass
class UpdateSlackStatusInput:
    slack_thread_context: dict[str, Any]
    text: str


@activity.defn
@close_db_connections
def update_slack_status(input: UpdateSlackStatusInput) -> None:
    """Update the live agent-status message in a Slack thread.

    Short, idempotent activity dispatched by ``SlackStatusRelayWorkflow`` once
    per coalesced status change. Find-existing-or-post handled by
    ``SlackThreadHandler.update_status``. Failure is logged and swallowed so a
    transient Slack outage never escalates to a task failure.
    """
    from products.slack_app.backend.slack_thread import SlackThreadContext, SlackThreadHandler

    try:
        context = SlackThreadContext.from_dict(input.slack_thread_context)
        SlackThreadHandler(context).update_status(input.text)
    except Exception as e:
        logger.warning("update_slack_status_failed", error=str(e))
