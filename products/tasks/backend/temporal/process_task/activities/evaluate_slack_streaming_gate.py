from dataclasses import dataclass
from typing import Any

from temporalio import activity

from posthog.temporal.common.logger import get_logger
from posthog.temporal.common.utils import close_db_connections

logger = get_logger(__name__)


@dataclass
class EvaluateSlackStreamingGateInput:
    team_id: int
    integration_id: int
    run_id: str


STREAMING_STATE_KEY = "slack_streaming_status_enabled"


@activity.defn
@close_db_connections
def evaluate_slack_streaming_gate(input: EvaluateSlackStreamingGateInput) -> bool:
    """Return True iff Slack agent-design streaming should run for this task.

    Wraps :func:`should_stream_slack_status` for use inside a Temporal workflow:
    the gate needs Django ORM access (to load the integration and check scopes)
    plus a posthoganalytics flag-eval network call, neither of which is safe
    inside a workflow handler.

    Also persists the decision on ``TaskRun.state[STREAMING_STATE_KEY]`` so
    later non-workflow callers (notably ``forward_pending_message``, which
    queues the legacy ``posthog-code-agent-relay`` for the final reply) can
    check it and skip duplicating the streamed narrative.
    """
    from posthog.models.integration import Integration

    from products.slack_app.backend.services.agent_status import should_stream_slack_status
    from products.tasks.backend.models import TaskRun

    try:
        integration = Integration.objects.get(id=input.integration_id)
    except Integration.DoesNotExist:
        logger.warning("slack_streaming_gate_integration_not_found", integration_id=input.integration_id)
        enabled = False
    else:
        enabled = should_stream_slack_status(input.team_id, integration)

    def _persist(state: dict[str, Any]) -> None:
        state[STREAMING_STATE_KEY] = enabled

    try:
        TaskRun.mutate_state_atomic(input.run_id, _persist)
    except Exception:
        logger.exception("slack_streaming_gate_state_persist_failed", run_id=input.run_id)
    return enabled
