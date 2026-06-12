from django.db import IntegrityError

import structlog

from posthog.models.github_integration_base import GitHubIntegrationBase
from posthog.models.organization import OrganizationMembership
from posthog.models.team.team import Team
from posthog.models.user_integration import UserGitHubIntegration

from products.signals.backend.report_generation.select_repo import resolve_team_github_integration
from products.tasks.backend.models import SandboxEnvironment, TaskRun

logger = structlog.get_logger(__name__)

SIGNALS_REPO_DISCOVERY_ENV_NAME = "SIGNALS_REPO_DISCOVERY"
SIGNALS_REPORT_RESEARCH_ENV_NAME = "SIGNALS_REPORT_RESEARCH"


def get_or_create_signals_sandbox_env(
    team_id: int,
    name: str,
    network_access_level: SandboxEnvironment.NetworkAccessLevel,
    *,
    allowed_domains: list[str] | None = None,
    include_default_domains: bool = False,
) -> str:
    """Get or create the internal SandboxEnvironment for a Signals agent. Returns the env ID as a string.

    Reasserts the expected policy on every call, so manual edits via the API are
    corrected on the next run. The lookup is scoped to ``internal=True`` so that a
    user-created environment that happens to share the name is never clobbered.

    Concurrent signal-report workflows for the same team race the get-then-create
    window in ``update_or_create``. Before the partial unique constraint existed,
    that race could leave two internal rows with the same ``(team, name)``, after
    which every subsequent lookup raised ``MultipleObjectsReturned`` and the team's
    reports failed permanently. We recover from both that legacy state and the
    constraint-era unique-violation race so a lost create is never fatal.
    """
    defaults: dict = {
        "network_access_level": network_access_level,
        "private": False,
    }
    if allowed_domains is not None:
        defaults["allowed_domains"] = allowed_domains
        defaults["include_default_domains"] = include_default_domains

    try:
        env, _ = SandboxEnvironment.objects.update_or_create(
            team_id=team_id,
            name=name,
            internal=True,
            defaults=defaults,
        )
        return str(env.id)
    except IntegrityError:
        # Lost a concurrent create race against the partial unique constraint. The
        # winner's row now exists; reassert the policy on it.
        return _reassert_canonical_signals_sandbox_env(team_id, name, defaults)
    except SandboxEnvironment.MultipleObjectsReturned:
        # Legacy duplicates created before the unique constraint existed.
        return _reassert_canonical_signals_sandbox_env(team_id, name, defaults, dedupe=True)


def _reassert_canonical_signals_sandbox_env(
    team_id: int,
    name: str,
    defaults: dict,
    *,
    dedupe: bool = False,
) -> str:
    """Pick the canonical (oldest) internal env for ``(team, name)``, reassert the
    policy on it, and — when ``dedupe`` is set — repoint references off the surplus
    duplicates and delete them. The canonical row is always kept.
    """
    duplicates = list(
        SandboxEnvironment.objects.filter(team_id=team_id, name=name, internal=True).order_by("created_at")
    )
    if not duplicates:
        # Another worker deleted everything between the failed call and now; retry once.
        env, _ = SandboxEnvironment.objects.update_or_create(
            team_id=team_id, name=name, internal=True, defaults=defaults
        )
        return str(env.id)

    canonical = duplicates[0]
    for field, value in defaults.items():
        setattr(canonical, field, value)
    canonical.save(update_fields=[*defaults.keys(), "updated_at"])

    if dedupe and len(duplicates) > 1:
        canonical_id = str(canonical.id)
        surplus_ids = [str(env.id) for env in duplicates[1:]]
        # Soft references live only in TaskRun.state JSON (no DB FK targets this model).
        # Repoint them to the canonical row before deleting the surplus.
        runs = list(TaskRun.objects.filter(state__sandbox_environment_id__in=surplus_ids))
        for run in runs:
            run.state["sandbox_environment_id"] = canonical_id
        if runs:
            TaskRun.objects.bulk_update(runs, ["state"])
        SandboxEnvironment.objects.filter(id__in=[env.id for env in duplicates[1:]]).delete()

    return str(canonical.id)


def resolve_user_id_for_team(team_id: int, github: GitHubIntegrationBase | None = None) -> int:
    """Resolve the best user ID for automated sandbox actions on behalf of a team.

    Pass `github` if the caller already resolved it to skip a duplicate query.
    """
    team = Team.objects.select_related("organization").get(id=team_id)
    if github is None:
        github = resolve_team_github_integration(team_id, team=team)
    if github is None:
        raise RuntimeError(f"No GitHub integration for team {team_id}; caller must short-circuit before calling this")
    # Pick the user who created the integration
    if isinstance(github, UserGitHubIntegration):
        return github.integration.user_id
    # If team-level Integration, prefer its creator (if still active in the org)
    if github.integration.created_by_id:
        is_active = OrganizationMembership.objects.filter(
            organization=team.organization,
            user_id=github.integration.created_by_id,
            user__is_active=True,
        ).exists()
        if is_active:
            return github.integration.created_by_id
        logger.warning(
            "github integration creator is no longer an active org member, falling back",
            team_id=team_id,
            integration_created_by=github.integration.created_by_id,
        )
    # Integration exists but its creator is gone — pick any active org member as a stand-in.
    membership = (
        OrganizationMembership.objects.select_related("user")
        .filter(organization=team.organization, user__is_active=True)
        .order_by("id")
        .first()
    )
    if not membership:
        raise RuntimeError(f"No active users in organization '{team.organization.name}' (team {team.id})")
    return membership.user_id
