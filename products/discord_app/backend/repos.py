from django.core.cache import cache

import structlog

from posthog.models.integration import Integration
from posthog.models.user_integration import UserGitHubIntegration, UserIntegration

logger = structlog.get_logger(__name__)

_MAX_GITHUB_REPOS = 500
REPO_LIST_CACHE_TTL_SECONDS = 300
# Discord autocomplete returns at most 25 choices.
MAX_AUTOCOMPLETE_CHOICES = 25


def _user_repo_list_cache_key(user_id: int) -> str:
    return f"discord_app:repo_list:{user_id}"


def invalidate_user_repo_list_cache(user_id: int) -> None:
    cache.delete(_user_repo_list_cache_key(user_id))


def get_full_repo_names(integration: Integration, *, user_id: int | None) -> list[str]:
    """Return canonical org/repo names from the user's personal GitHub install, or [].

    Repos are scoped to the user's personal GitHub integration so the picker matches the
    identity that will author the resulting pull request. Mirrors the Slack bridge.
    """
    if user_id is None:
        return []
    cache_key = _user_repo_list_cache_key(user_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    user_records = UserIntegration.objects.filter(user_id=user_id, kind=UserIntegration.IntegrationKind.GITHUB)
    if not user_records.exists():
        cache.set(cache_key, [], timeout=REPO_LIST_CACHE_TTL_SECONDS)
        return []

    all_repos: set[str] = set()
    for record in user_records:
        github = UserGitHubIntegration(record)
        for repo in github.list_all_cached_repositories(max_repos=_MAX_GITHUB_REPOS):
            all_repos.add(repo["full_name"])
            if len(all_repos) >= _MAX_GITHUB_REPOS:
                logger.warning("discord_github_repo_list_capped", user_id=user_id, team_id=integration.team_id)
                result = sorted(all_repos)
                cache.set(cache_key, result, timeout=REPO_LIST_CACHE_TTL_SECONDS)
                return result

    result = sorted(all_repos)
    cache.set(cache_key, result, timeout=REPO_LIST_CACHE_TTL_SECONDS)
    return result


def repo_autocomplete_choices(integration: Integration, *, user_id: int | None, query: str) -> list[dict[str, str]]:
    """Discord autocomplete choices ([{name, value}]) filtered by the partial query."""
    needle = (query or "").lower().strip()
    names = get_full_repo_names(integration, user_id=user_id)
    matches = [name for name in names if needle in name.lower()] if needle else names
    return [{"name": name, "value": name} for name in matches[:MAX_AUTOCOMPLETE_CHOICES]]
