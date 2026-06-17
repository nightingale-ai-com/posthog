"""Gate helper for the Slack agent-design streaming-status feature.

Enables a live ``chat_update``-driven status message in the Slack thread
(replacing the static "Working on task…" placeholder) when the feature flag is
on for the workspace and the integration carries the required Slack scope.

Region-aware feature flag — same idiom as ``UNTAGGED_THREAD_FOLLOWUPS_FLAG``
and the assistant DM flag in ``products/slack_app/backend/api.py``.
"""

import structlog
import posthoganalytics

from posthog.models.integration import Integration, SlackIntegration
from posthog.utils import get_instance_region

logger = structlog.get_logger(__name__)

AGENT_STATUS_FLAG = "posthog-code-slack-agent-status"
REQUIRED_SCOPES = frozenset({"chat:write"})


def should_stream_slack_status(team_id: int, integration: Integration) -> bool:
    """True when live agent-status streaming should run for this task.

    Fail-closed on any error — a transient PostHog API outage must not silently
    enable the feature for everyone.
    """
    if SlackIntegration(integration).missing_scopes(REQUIRED_SCOPES):
        return False
    try:
        enabled = posthoganalytics.feature_enabled(
            AGENT_STATUS_FLAG,
            f"team:{team_id}",
            groups={"organization": str(integration.team.organization_id)},
            person_properties={"region": get_instance_region() or "unknown"},
            only_evaluate_locally=False,
            send_feature_flag_events=False,
        )
        return bool(enabled)
    except Exception:
        logger.exception("agent_status_flag_eval_failed", team_id=team_id)
        return False
