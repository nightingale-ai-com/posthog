from posthog.models.integration import Integration
from posthog.models.repo_routing_rule import RepoRoutingRule
from posthog.models.user import User

from products.discord_app.backend.models import DiscordSettings


def handle_project_show(integration: Integration) -> str:
    return f"This channel routes to project `{integration.team_id}` — {integration.team.name}."


def handle_project_set(*, guild_id: str, discord_user_id: str, integration: Integration) -> str:
    """Set the invoking Discord user's personal default project for this guild."""
    DiscordSettings.objects.update_or_create(
        guild_id=guild_id,
        discord_user_id=discord_user_id,
        defaults={"default_integration": integration},
    )
    return f"Your default project for this server is now `{integration.team_id}` — {integration.team.name}."


def handle_project_set_workspace(*, guild_id: str, integration: Integration) -> str:
    """Set the guild-wide default project (caller permission is enforced upstream)."""
    DiscordSettings.objects.update_or_create(
        guild_id=guild_id,
        discord_user_id=None,
        defaults={"default_integration": integration},
    )
    return f"The server-wide default project is now `{integration.team_id}` — {integration.team.name}."


def handle_rules_list(integration: Integration) -> str:
    rules = list(RepoRoutingRule.objects.filter(team_id=integration.team_id).order_by("priority", "id"))
    if not rules:
        return "No routing rules configured. Add one with `/posthog-rules add`."
    lines = [f"{idx + 1}. {r.rule_text} → `{r.repository}`" for idx, r in enumerate(rules)]
    return "Routing rules:\n" + "\n".join(lines)


def handle_rules_add(*, integration: Integration, user: User, text: str, repository: str) -> str:
    text = (text or "").strip()
    repository = (repository or "").strip()
    if not text or not repository:
        return "Provide both a description and a repository (`owner/repo`)."
    RepoRoutingRule.objects.create(
        team_id=integration.team_id,
        rule_text=text,
        repository=repository,
        created_by=user,
    )
    return f"Added rule: {text} → `{repository}`."


def handle_rules_remove(*, integration: Integration, ids: str) -> str:
    rules = list(RepoRoutingRule.objects.filter(team_id=integration.team_id).order_by("priority", "id"))
    try:
        indices = {int(part.strip()) for part in ids.split(",") if part.strip()}
    except ValueError:
        return "Provide rule numbers as a comma-separated list, e.g. `1,3`."
    to_delete = [rules[i - 1] for i in sorted(indices) if 1 <= i <= len(rules)]
    if not to_delete:
        return "No matching rules to remove."
    RepoRoutingRule.objects.filter(id__in=[r.id for r in to_delete]).delete()
    return f"Removed {len(to_delete)} rule(s)."
