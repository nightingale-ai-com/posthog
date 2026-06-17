from dataclasses import dataclass

from temporalio import activity

from posthog.temporal.common.logger import get_logger
from posthog.temporal.common.utils import close_db_connections

logger = get_logger(__name__)


@dataclass
class EvaluateSlackStreamingGateInput:
    team_id: int
    integration_id: int


@activity.defn
@close_db_connections
def evaluate_slack_streaming_gate(input: EvaluateSlackStreamingGateInput) -> bool:
    """Return True iff Slack agent-design streaming should run for this task.

    Wraps :func:`should_stream_slack_status` for use inside a Temporal workflow:
    the gate needs Django ORM access (to load the integration and check scopes)
    plus a posthoganalytics flag-eval network call, neither of which is safe
    inside a workflow handler.
    """
    from posthog.models.integration import Integration

    from products.slack_app.backend.services.agent_status import should_stream_slack_status

    try:
        integration = Integration.objects.get(id=input.integration_id)
    except Integration.DoesNotExist:
        logger.warning("slack_streaming_gate_integration_not_found", integration_id=input.integration_id)
        return False
    return should_stream_slack_status(input.team_id, integration)
