import json
import asyncio
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import csrf_exempt

import requests
import structlog
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from posthog.models.integration import DiscordIntegrationError, Integration, verify_discord_bridge_bearer
from posthog.models.organization import OrganizationMembership
from posthog.temporal.ai.posthog_code_discord_interactivity import (
    PostHogCodeDiscordInteractivityInputs,
    PostHogCodeDiscordTerminateTaskWorkflow,
)
from posthog.temporal.ai.posthog_code_discord_mention import (
    REPO_NONE_CUSTOM_ID,
    REPO_SELECT_CUSTOM_ID,
    PostHogCodeDiscordMentionWorkflow,
    PostHogCodeDiscordMentionWorkflowInputs,
    derive_discord_mention_workflow_id,
)
from posthog.temporal.common.client import sync_connect

from products.discord_app.backend.models import DiscordUserLink
from products.discord_app.backend.repos import repo_autocomplete_choices
from products.discord_app.backend.services import commands as commands_dispatch
from products.discord_app.backend.services.integration_resolver import (
    ResolutionResult,
    format_project_candidate_list,
    load_integrations,
)

logger = structlog.get_logger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
ACCOUNT_LINK_SALT = "discord_app:account_link"
ACCOUNT_LINK_MAX_AGE_SECONDS = 900
TERMINATE_CUSTOM_ID = "posthog_code_terminate_task"


def _ok(**fields: Any) -> JsonResponse:
    return JsonResponse({"status": "accepted", **fields})


def _ephemeral(content: str) -> JsonResponse:
    return JsonResponse({"action": "ephemeral", "content": content})


def _start_workflow(workflow_cls: Any, inputs: Any, workflow_id: str) -> None:
    client = sync_connect()
    asyncio.run(
        client.start_workflow(
            workflow_cls.run,
            inputs,
            id=workflow_id,
            task_queue=settings.MAX_AI_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    )


def _signal_workflow(workflow_id: str, signal: Any, *args: Any) -> None:
    client = sync_connect()
    handle = client.get_workflow_handle(workflow_id)
    asyncio.run(handle.signal(signal, args=list(args)))


def _account_link_url(*, integration_id: int, discord_user_id: str) -> str:
    state = signing.dumps(
        {"integration_id": integration_id, "discord_user_id": discord_user_id},
        salt=ACCOUNT_LINK_SALT,
    )
    return f"{settings.SITE_URL}/api/discord/oauth/link/start?{urlencode({'state': state})}"


@csrf_exempt
def discord_interactions_ingest(request: HttpRequest) -> HttpResponse:
    """Receive forwarded Discord interactions from the companion bot (static-bearer authed)."""
    try:
        verify_discord_bridge_bearer(request)
    except DiscordIntegrationError:
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body)
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "bad request"}, status=400)

    kind = payload.get("kind")
    if kind == "command":
        return _handle_command(payload)
    if kind == "component":
        return _handle_component(payload)
    if kind == "modal_submit":
        # Follow-up modal text — forwarding to a running sandbox is handled out of band.
        return _ok()
    return JsonResponse({"error": "unknown kind"}, status=400)


def _resolve_integration(payload: dict[str, Any]) -> ResolutionResult:
    return load_integrations(
        guild_id=payload.get("guild_id", ""),
        discord_user_id=(payload.get("user") or {}).get("id", ""),
        channel_id=payload.get("channel_id"),
        thread_id=payload.get("channel_id"),
    )


def _handle_command(payload: dict[str, Any]) -> HttpResponse:
    command = payload.get("command")
    user = payload.get("user") or {}
    discord_user_id = user.get("id", "")
    guild_id = payload.get("guild_id", "")

    resolution = _resolve_integration(payload)
    integration = resolution.integration

    # `/posthog-project` must work without a resolved default — it's the command that sets one.
    if command == "posthog-project":
        return _handle_project_command(payload, resolution, guild_id, discord_user_id)

    if integration is None:
        if not resolution.candidates:
            return _ephemeral("This server isn't connected to PostHog yet.")
        return _ephemeral("Multiple PostHog projects are connected. Set yours with `/posthog-project set <id>`.")

    if command == "posthog":
        return _handle_posthog_command(payload, integration, guild_id, discord_user_id)
    if command == "posthog-rules":
        return _handle_rules_command(payload, integration, discord_user_id)
    return JsonResponse({"error": "unknown command"}, status=400)


def _linked_user(integration: Integration, discord_user_id: str) -> DiscordUserLink | None:
    return DiscordUserLink.objects.filter(integration=integration, discord_user_id=discord_user_id).first()


def _handle_posthog_command(
    payload: dict[str, Any], integration: Integration, guild_id: str, discord_user_id: str
) -> HttpResponse:
    link = _linked_user(integration, discord_user_id)
    if link is None:
        url = _account_link_url(integration_id=integration.id, discord_user_id=discord_user_id)
        return _ephemeral(f"First, link your PostHog account: {url}")

    inputs = PostHogCodeDiscordMentionWorkflowInputs(
        interaction={
            "channel_id": payload.get("channel_id"),
            "message_id": payload.get("message_id"),
            "options": payload.get("options") or {},
            "interaction_id": payload.get("interaction_id"),
            "interaction_token": payload.get("interaction_token"),
        },
        integration_id=integration.id,
        guild_id=guild_id,
        user_id=link.user_id,
        discord_user_id=discord_user_id,
    )
    try:
        _start_workflow(PostHogCodeDiscordMentionWorkflow, inputs, derive_discord_mention_workflow_id(inputs))
    except Exception:
        logger.exception("discord_start_mention_workflow_failed", guild_id=guild_id)
        return _ephemeral("Sorry, I ran into an internal error starting the task. Please try again.")
    return _ok()


def _handle_rules_command(payload: dict[str, Any], integration: Integration, discord_user_id: str) -> HttpResponse:
    link = _linked_user(integration, discord_user_id)
    if link is None:
        return _ephemeral("Link your PostHog account first with `/posthog`.")
    options = payload.get("options") or {}
    sub = payload.get("subcommand")
    if sub == "list":
        return _ephemeral(commands_dispatch.handle_rules_list(integration))
    if sub == "add":
        return _ephemeral(
            commands_dispatch.handle_rules_add(
                integration=integration,
                user=link.user,
                text=options.get("text", ""),
                repository=options.get("repo", ""),
            )
        )
    if sub == "remove":
        return _ephemeral(commands_dispatch.handle_rules_remove(integration=integration, ids=options.get("ids", "")))
    return _ephemeral("Usage: `/posthog-rules list|add|remove`.")


def _handle_project_command(
    payload: dict[str, Any], resolution: ResolutionResult, guild_id: str, discord_user_id: str
) -> HttpResponse:
    options = payload.get("options") or {}
    sub = payload.get("subcommand")
    if sub == "show" or sub is None:
        if resolution.integration is not None:
            return _ephemeral(commands_dispatch.handle_project_show(resolution.integration))
        if not resolution.candidates:
            return _ephemeral("This server isn't connected to PostHog yet.")
        return _ephemeral(
            "No default project set. Connected projects:\n"
            + format_project_candidate_list(resolution.candidates)
            + "\n\nSet yours with `/posthog-project set <id>`."
        )

    target = _integration_for_project_id(guild_id, options.get("project_id", ""))
    if target is None:
        return _ephemeral("That project isn't connected to this server.")

    if sub == "set":
        return _ephemeral(
            commands_dispatch.handle_project_set(guild_id=guild_id, discord_user_id=discord_user_id, integration=target)
        )
    if sub == "workspace":
        if not _is_org_admin(target, discord_user_id):
            return _ephemeral("Setting the server-wide default requires being a PostHog org admin.")
        return _ephemeral(commands_dispatch.handle_project_set_workspace(guild_id=guild_id, integration=target))
    return _ephemeral("Usage: `/posthog-project show|set|workspace`.")


def _integration_for_project_id(guild_id: str, project_id: str) -> Integration | None:
    project_id = (project_id or "").strip()
    if not project_id.isdigit():
        return None
    return Integration.objects.filter(kind="discord", integration_id=guild_id, team_id=int(project_id)).first()


def _is_org_admin(integration: Integration, discord_user_id: str) -> bool:
    link = _linked_user(integration, discord_user_id)
    if link is None:
        return False
    membership = OrganizationMembership.objects.filter(
        user=link.user, organization_id=integration.team.organization_id
    ).first()
    return membership is not None and membership.level >= OrganizationMembership.Level.ADMIN


def _handle_component(payload: dict[str, Any]) -> HttpResponse:
    custom_id = payload.get("custom_id", "")
    name, _, workflow_id = custom_id.partition(":")

    if name == REPO_SELECT_CUSTOM_ID and workflow_id:
        values = payload.get("values") or []
        if values:
            try:
                _signal_workflow(workflow_id, PostHogCodeDiscordMentionWorkflow.repo_selected, values[0])
            except Exception:
                logger.exception("discord_repo_selected_signal_failed", workflow_id=workflow_id)
        return _ok()

    if name == REPO_NONE_CUSTOM_ID and workflow_id:
        try:
            _signal_workflow(workflow_id, PostHogCodeDiscordMentionWorkflow.no_repo_needed)
        except Exception:
            logger.exception("discord_no_repo_signal_failed", workflow_id=workflow_id)
        return _ok()

    if name == TERMINATE_CUSTOM_ID and workflow_id:
        run_id = workflow_id  # custom_id is posthog_code_terminate_task:{run_id}
        inputs = PostHogCodeDiscordInteractivityInputs(payload=payload)
        try:
            _start_workflow(
                PostHogCodeDiscordTerminateTaskWorkflow,
                inputs,
                f"posthog-code-discord-terminate-{payload.get('guild_id', '')}:{run_id}",
            )
        except Exception:
            logger.exception("discord_terminate_start_failed", run_id=run_id)
        return _ok()

    return _ok()


@csrf_exempt
def discord_repos(request: HttpRequest) -> HttpResponse:
    """Repo-name autocomplete for the companion bot (static-bearer authed, ~2 s budget)."""
    try:
        verify_discord_bridge_bearer(request)
    except DiscordIntegrationError:
        return JsonResponse({"choices": []}, status=401)

    guild_id = request.GET.get("guild_id", "")
    discord_user_id = request.GET.get("user_id", "")
    query = request.GET.get("query", "")

    resolution = load_integrations(guild_id=guild_id, discord_user_id=discord_user_id)
    integration = resolution.integration
    if integration is None:
        return JsonResponse({"choices": []})

    link = _linked_user(integration, discord_user_id)
    if link is None:
        return JsonResponse({"choices": []})

    choices = repo_autocomplete_choices(integration, user_id=link.user_id, query=query)
    return JsonResponse({"choices": choices})


def discord_oauth_link_start(request: HttpRequest) -> HttpResponse:
    """Begin account-linking: requires a PostHog session, then redirects to Discord OAuth."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(f"/login?next={request.get_full_path()}")
    try:
        data = signing.loads(request.GET.get("state", ""), salt=ACCOUNT_LINK_SALT, max_age=ACCOUNT_LINK_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return JsonResponse({"error": "invalid or expired link"}, status=400)

    state = signing.dumps(
        {
            "integration_id": data["integration_id"],
            "discord_user_id": data["discord_user_id"],
            "user_id": request.user.id,
        },
        salt=ACCOUNT_LINK_SALT,
    )
    params = {
        "client_id": settings.DISCORD_APP_CLIENT_ID,
        "response_type": "code",
        "scope": "identify",
        "redirect_uri": f"{settings.SITE_URL}/api/discord/oauth/link/callback",
        "state": state,
    }
    return HttpResponseRedirect(f"https://discord.com/oauth2/authorize?{urlencode(params)}")


def discord_oauth_link_callback(request: HttpRequest) -> HttpResponse:
    """Complete account-linking: verify the Discord identity and persist DiscordUserLink."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "not authenticated"}, status=403)
    try:
        data = signing.loads(request.GET.get("state", ""), salt=ACCOUNT_LINK_SALT, max_age=ACCOUNT_LINK_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return JsonResponse({"error": "invalid or expired link"}, status=400)

    if data.get("user_id") != request.user.id:
        return JsonResponse({"error": "session mismatch"}, status=403)

    code = request.GET.get("code", "")
    if not code:
        return JsonResponse({"error": "missing code"}, status=400)

    try:
        token_resp = requests.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": settings.DISCORD_APP_CLIENT_ID,
                "client_secret": settings.DISCORD_APP_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.SITE_URL}/api/discord/oauth/link/callback",
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
        me_resp = requests.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_resp.raise_for_status()
        me = me_resp.json()
    except Exception:
        logger.exception("discord_oauth_link_exchange_failed")
        return JsonResponse({"error": "discord oauth failed"}, status=502)

    if me.get("id") != data["discord_user_id"]:
        return JsonResponse({"error": "discord identity mismatch"}, status=403)

    integration = Integration.objects.filter(id=data["integration_id"], kind="discord").first()
    if integration is None:
        return JsonResponse({"error": "integration not found"}, status=404)

    DiscordUserLink.objects.update_or_create(
        integration=integration,
        discord_user_id=data["discord_user_id"],
        defaults={"user": request.user, "discord_username": me.get("username", "")},
    )
    return HttpResponse("Your PostHog account is now linked to Discord. You can close this tab.")
