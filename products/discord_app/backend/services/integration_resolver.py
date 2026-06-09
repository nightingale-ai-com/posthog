from dataclasses import dataclass, field
from typing import Literal

from django.db.models import Q

from posthog.models.integration import Integration
from posthog.models.user import User

from products.discord_app.backend.models import DiscordSettings, DiscordThreadTaskMapping

ResolutionSource = Literal[
    "thread",
    "user_default",
    "guild_default",
    "sole_candidate",
    "needs_picker",
]


@dataclass
class ResolutionResult:
    integration: Integration | None
    source: ResolutionSource
    candidates: list[Integration] = field(default_factory=list)


def format_project_candidate_list(candidates: list[Integration]) -> str:
    return "\n".join(f"• `{c.team_id}` — {c.team.organization.name} · {c.team.name}" for c in candidates)


def resolve_from_candidates(
    candidates: list[Integration],
    *,
    guild_id: str,
    discord_user_id: str = "",
    user: User | None = None,
    channel_id: str | None = None,
    thread_id: str | None = None,
) -> ResolutionResult:
    """Run the routing resolver over a pre-loaded candidate list.

    Precedence: ``thread`` > ``user_default`` > ``guild_default`` > ``sole_candidate`` >
    ``needs_picker``. Mirrors the Slack bridge's resolver.
    """
    accessible_team_ids: set[int] | None = set(user.teams.values_list("id", flat=True)) if user is not None else None
    accessible = (
        candidates if accessible_team_ids is None else [i for i in candidates if i.team_id in accessible_team_ids]
    )
    candidate_ids = {c.id for c in candidates}
    candidates_by_team_id = {c.team_id: c for c in candidates}

    if channel_id and thread_id and guild_id:
        # Bridge lookup has no team context; .unscoped() is the documented escape hatch.
        thread_match = (
            DiscordThreadTaskMapping.objects.unscoped()
            .filter(guild_id=guild_id, channel_id=channel_id, thread_id=thread_id)
            .select_related("integration")
            .first()
        )
        if thread_match is not None:
            mapped = thread_match.integration
            target: Integration | None = (
                mapped if mapped.id in candidate_ids else candidates_by_team_id.get(mapped.team_id)
            )
            if target is not None and (accessible_team_ids is None or target.team_id in accessible_team_ids):
                return ResolutionResult(integration=target, source="thread", candidates=accessible)

    if discord_user_id:
        defaults = list(
            DiscordSettings.objects.filter(guild_id=guild_id)
            .filter(Q(discord_user_id=discord_user_id) | Q(discord_user_id__isnull=True))
            .select_related(
                "default_integration",
                "default_integration__team",
                "default_integration__team__organization",
            )
        )
        defaults.sort(key=lambda d: d.discord_user_id is None)
        for default in defaults:
            if accessible_team_ids is not None and default.default_integration.team_id not in accessible_team_ids:
                continue
            if default.default_integration.id not in candidate_ids:
                continue
            source: ResolutionSource = "user_default" if default.discord_user_id else "guild_default"
            return ResolutionResult(integration=default.default_integration, source=source, candidates=accessible)

    if len(accessible) == 1:
        return ResolutionResult(integration=accessible[0], source="sole_candidate", candidates=accessible)
    return ResolutionResult(integration=None, source="needs_picker", candidates=accessible)


def load_integrations(
    *,
    guild_id: str,
    discord_user_id: str = "",
    user: User | None = None,
    channel_id: str | None = None,
    thread_id: str | None = None,
) -> ResolutionResult:
    """Load Discord integrations for a guild, then run the routing resolver against them."""
    candidates = list(
        Integration.objects.filter(kind="discord", integration_id=guild_id)
        .select_related("team", "team__organization", "created_by")
        .order_by("id")
    )
    return resolve_from_candidates(
        candidates,
        guild_id=guild_id,
        discord_user_id=discord_user_id,
        user=user,
        channel_id=channel_id,
        thread_id=thread_id,
    )
