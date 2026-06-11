import json
import asyncio
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt

import requests
import structlog
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from posthog.models.integration import (
    DiscordBotClient,
    DiscordIntegrationError,
    Integration,
    discord_deployment_region,
    verify_discord_bridge_bearer,
)
from posthog.models.organization import OrganizationMembership
from posthog.models.team import Team
from posthog.models.user import User
from posthog.temporal.ai.posthog_code_discord_followup import (
    PostHogCodeDiscordFollowupInputs,
    PostHogCodeDiscordFollowupWorkflow,
)
from posthog.temporal.ai.posthog_code_discord_forum import (
    PostHogCodeDiscordForumInputs,
    PostHogCodeDiscordForumTriageWorkflow,
    derive_discord_forum_workflow_id,
)
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

from products.discord_app.backend.models import DiscordSettings, DiscordThreadTaskMapping, DiscordUserLink
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
SERVER_CONNECT_SALT = "discord_app:server_connect"
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


def _server_connect_url(*, guild_id: str, guild_name: str, discord_user_id: str, project_id_hint: str = "") -> str:
    state = signing.dumps(
        {
            "guild_id": guild_id,
            "guild_name": guild_name,
            "discord_user_id": discord_user_id,
            "project_id_hint": str(project_id_hint or ""),
        },
        salt=SERVER_CONNECT_SALT,
    )
    return f"{settings.SITE_URL}/api/discord/connect/start?{urlencode({'state': state})}"


def _admin_teams(user: User) -> list[Team]:
    """Projects the user may connect a guild to: teams in orgs where they are org admin."""
    admin_org_ids = OrganizationMembership.objects.filter(
        user=user, level__gte=OrganizationMembership.Level.ADMIN
    ).values_list("organization_id", flat=True)
    return list(user.teams.filter(organization_id__in=admin_org_ids).select_related("organization").order_by("id"))


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
    if kind == "message":
        # Plain replies in PostHog-managed threads, forwarded by the bot.
        return _handle_thread_message(payload)
    if kind == "forum_post":
        # New posts in watched forum channels — automatic triage.
        return _handle_forum_post(payload)
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

    # `/ph connect` bootstraps the guild ↔ project mapping, so it must work before anything
    # is connected (no resolution needed — also keeps the reply inside the bot's ~10s budget).
    # Completing the link requires PostHog org admin, checked web-side.
    if command in ("connect", "posthog-connect"):
        url = _server_connect_url(
            guild_id=guild_id,
            guild_name=payload.get("guild_name", ""),
            discord_user_id=discord_user_id,
            project_id_hint=(payload.get("options") or {}).get("project_id", ""),
        )
        return _ephemeral(f"Connect this server: {url}")

    resolution = _resolve_integration(payload)
    integration = resolution.integration

    # `/ph project` must work without a resolved default — it's the command that sets one.
    if command in ("project", "posthog-project"):
        return _handle_project_command(payload, resolution, guild_id, discord_user_id)

    if integration is None:
        if not resolution.candidates:
            return _ephemeral("This server isn't connected to PostHog yet. Connect it with `/ph connect`.")
        # The bot ships no project-picker command, so reconnecting is the routing escape hatch.
        return _ephemeral(
            "Multiple PostHog projects are connected to this server. Re-run `/ph connect` to choose which one to use."
        )

    # The bot relays `/ph <subcommand>` as command=<subcommand>; the posthog-* spellings
    # predate that convention and are kept as aliases.
    if command in ("code", "posthog"):
        # Inside an existing task thread, `/ph code` is a follow-up to that task —
        # threads can't nest, so starting a fresh task there would fail anyway.
        mapping = (
            DiscordThreadTaskMapping.objects.unscoped()
            .filter(guild_id=guild_id, thread_id=payload.get("channel_id") or "")
            .select_related("integration", "task_run")
            .first()
        )
        if mapping is not None:
            return _handle_followup_command(payload, mapping, discord_user_id)
        return _handle_posthog_command(payload, integration, guild_id, discord_user_id)
    if command in ("rules", "posthog-rules"):
        return _handle_rules_command(payload, integration, discord_user_id)
    logger.info("discord_unknown_command", command=command, guild_id=guild_id)
    return JsonResponse({"error": f"unknown command: {command}"}, status=400)


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


def _followup_display_name(user: dict[str, Any]) -> str:
    return user.get("global_name") or user.get("username") or "A teammate"


def _start_followup_workflow(
    *, guild_id: str, thread_id: str, text: str, discord_user_id: str, dedupe_key: str, message_id: str | None = None
) -> None:
    inputs = PostHogCodeDiscordFollowupInputs(
        guild_id=guild_id,
        thread_id=thread_id,
        text=text,
        discord_user_id=discord_user_id,
        message_id=message_id,
    )
    _start_workflow(
        PostHogCodeDiscordFollowupWorkflow,
        inputs,
        f"posthog-code-discord-followup-{guild_id}:{dedupe_key}",
    )


def _handle_followup_command(payload: dict[str, Any], mapping: Any, discord_user_id: str) -> HttpResponse:
    link = _linked_user(mapping.integration, discord_user_id)
    if link is None:
        url = _account_link_url(integration_id=mapping.integration_id, discord_user_id=discord_user_id)
        return _ephemeral(f"First, link your PostHog account: {url}")

    text = ((payload.get("options") or {}).get("prompt") or "").strip()
    if not text:
        return _ephemeral("Add a prompt: `/ph code <message for the running agent>`.")
    if discord_user_id != mapping.discord_user_id:
        text = f"{_followup_display_name(payload.get('user') or {})}: {text}"

    try:
        _start_followup_workflow(
            guild_id=mapping.guild_id,
            thread_id=mapping.thread_id,
            text=text,
            discord_user_id=discord_user_id,
            dedupe_key=payload.get("interaction_id") or mapping.thread_id,
        )
    except Exception:
        logger.exception("discord_followup_workflow_start_failed", thread_id=mapping.thread_id)
        return _ephemeral("Sorry, I couldn't reach the running agent. Please try again.")
    return _ephemeral("Sent to the running agent \U0001f440")


def _handle_thread_message(payload: dict[str, Any]) -> HttpResponse:
    """Forward a plain message in a PostHog-managed thread to the running agent.

    Quietly ignores anything that isn't a follow-up: bot/self messages, threads we
    don't manage, and senders who haven't linked their PostHog account.
    """
    user = payload.get("user") or {}
    if user.get("bot"):
        return _ok()
    content = (payload.get("content") or "").strip()
    guild_id = payload.get("guild_id") or ""
    thread_id = payload.get("channel_id") or ""
    if not content or not guild_id or not thread_id:
        return _ok()

    mapping = (
        DiscordThreadTaskMapping.objects.unscoped()
        .filter(guild_id=guild_id, thread_id=thread_id)
        .select_related("integration")
        .first()
    )
    if mapping is None:
        return _ok()

    discord_user_id = user.get("id", "")
    # The thread owner may reply without a linked account: forum-triage threads belong to
    # community members, and the task already runs under the connecting user's identity.
    is_thread_owner = discord_user_id and discord_user_id == mapping.discord_user_id
    if not is_thread_owner and _linked_user(mapping.integration, discord_user_id) is None:
        logger.info("discord_thread_message_unlinked_sender", thread_id=thread_id)
        return _ok()

    text = content
    if discord_user_id != mapping.discord_user_id:
        text = f"{_followup_display_name(user)}: {text}"

    try:
        _start_followup_workflow(
            guild_id=guild_id,
            thread_id=thread_id,
            text=text,
            discord_user_id=discord_user_id,
            dedupe_key=payload.get("message_id") or payload.get("interaction_id") or thread_id,
            message_id=payload.get("message_id"),
        )
    except Exception:
        logger.exception("discord_thread_message_forward_failed", thread_id=thread_id)
    return _ok()


def _forum_rate_cap_exceeded(guild_id: str) -> bool:
    """Per-guild hourly cap on automatic forum triages — a post flood must not drain
    AI credits. Sliding hour bucket in the cache; fail open if the cache is down."""
    try:
        key = f"discord_app:forum_triage_cap:{guild_id}"
        count = cache.get_or_set(key, 0, timeout=3600)
        if count >= settings.DISCORD_FORUM_TRIAGE_HOURLY_CAP:
            return True
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=3600)
        return False
    except Exception:
        logger.exception("discord_forum_cap_check_failed", guild_id=guild_id)
        return False


def _handle_forum_post(payload: dict[str, Any]) -> HttpResponse:
    """Kick off automatic triage for a new forum post. The author is a community member,
    so the task runs under the service identity of whoever connected the guild."""
    author = payload.get("author") or {}
    if author.get("bot"):
        return JsonResponse({"status": "skipped", "reason": "bot author"})

    guild_id = payload.get("guild_id") or ""
    thread_id = payload.get("thread_id") or ""
    if not guild_id or not thread_id:
        return JsonResponse({"error": "bad request"}, status=400)

    resolution = load_integrations(guild_id=guild_id)
    integration = resolution.integration
    if integration is None:
        return JsonResponse({"status": "skipped", "reason": "guild not connected"})

    service_user_id = integration.created_by_id
    if service_user_id is None:
        logger.warning("discord_forum_post_no_service_identity", guild_id=guild_id)
        return JsonResponse({"status": "skipped", "reason": "no service identity"})

    if _forum_rate_cap_exceeded(guild_id):
        logger.warning("discord_forum_post_rate_capped", guild_id=guild_id, thread_id=thread_id)
        return JsonResponse({"status": "skipped", "reason": "rate capped"})

    inputs = PostHogCodeDiscordForumInputs(
        integration_id=integration.id,
        guild_id=guild_id,
        forum_channel_id=payload.get("forum_channel_id") or "",
        thread_id=thread_id,
        title=payload.get("title") or "",
        content=payload.get("content") or "",
        user_id=service_user_id,
        author_discord_user_id=author.get("id", ""),
        tags=[t for t in (payload.get("tags") or []) if isinstance(t, str)],
    )
    try:
        # Workflow id is keyed on the thread, so bot retries and duplicate events no-op.
        _start_workflow(
            PostHogCodeDiscordForumTriageWorkflow,
            inputs,
            derive_discord_forum_workflow_id(guild_id, thread_id),
        )
    except Exception:
        logger.exception("discord_forum_workflow_start_failed", guild_id=guild_id, thread_id=thread_id)
        return JsonResponse({"error": "failed to start triage"}, status=503)
    return _ok()


def _handle_rules_command(payload: dict[str, Any], integration: Integration, discord_user_id: str) -> HttpResponse:
    link = _linked_user(integration, discord_user_id)
    if link is None:
        return _ephemeral("Link your PostHog account first with `/ph code`.")
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
    return _ephemeral("Usage: `/ph rules list|add|remove`.")


def _handle_project_command(
    payload: dict[str, Any], resolution: ResolutionResult, guild_id: str, discord_user_id: str
) -> HttpResponse:
    options = payload.get("options") or {}
    sub = payload.get("subcommand")
    if sub == "show" or sub is None:
        if resolution.integration is not None:
            return _ephemeral(commands_dispatch.handle_project_show(resolution.integration))
        if not resolution.candidates:
            return _ephemeral("This server isn't connected to PostHog yet. Connect it with `/ph connect`.")
        return _ephemeral(
            "No default project set. Connected projects:\n"
            + format_project_candidate_list(resolution.candidates)
            + "\n\nSet yours with `/ph project set <id>`."
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
    return _ephemeral("Usage: `/ph project show|set|workspace`.")


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


def discord_connect_start(request: HttpRequest) -> HttpResponse:
    """Begin server-connect from `/ph connect`: requires a PostHog session, then shows a
    project picker limited to orgs where the user is an admin."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(f"/login?next={request.get_full_path()}")
    try:
        data = signing.loads(
            request.GET.get("state", ""), salt=SERVER_CONNECT_SALT, max_age=ACCOUNT_LINK_MAX_AGE_SECONDS
        )
    except signing.BadSignature:
        return JsonResponse({"error": "invalid or expired link"}, status=400)

    teams = _admin_teams(request.user)
    if not teams:
        return HttpResponse("Connecting a Discord server requires being an organization admin in PostHog.", status=403)

    # Re-sign with the session user baked in so confirm can detect a session swap mid-flow.
    confirm_state = signing.dumps({**data, "user_id": request.user.id}, salt=SERVER_CONNECT_SALT)

    # Pre-select the /ph connect project_id hint when valid, else the user's current project.
    hint = data.get("project_id_hint") or ""
    team_ids = {team.id for team in teams}
    preselected = int(hint) if hint.isdigit() and int(hint) in team_ids else request.user.current_team_id
    options = "".join(
        f'<option value="{team.id}"{" selected" if team.id == preselected else ""}>'
        f"{escape(team.organization.name)} · {escape(team.name)} (id {team.id})</option>"
        for team in teams
    )
    guild_label = escape(data.get("guild_name") or data["guild_id"])
    html = f"""<!doctype html><html><head><title>Connect Discord server</title></head>
<body style="font-family: sans-serif; max-width: 32rem; margin: 4rem auto;">
<h2>Connect Discord server</h2>
<p>Connect the Discord server <strong>{guild_label}</strong> to a PostHog project. Tasks started
from that server will run in the selected project, and PostHog data may be surfaced there.</p>
<form method="post" action="/api/discord/connect/confirm">
<input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
<input type="hidden" name="state" value="{escape(confirm_state)}">
<label>Project:<br><select name="team_id" style="margin: 0.5rem 0;">{options}</select></label><br>
<button type="submit">Connect server</button>
</form>
</body></html>"""
    return HttpResponse(html)


def discord_connect_confirm(request: HttpRequest) -> HttpResponse:
    """Complete server-connect: verify state + org admin, then persist the Integration row."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({"error": "not authenticated"}, status=403)
    try:
        data = signing.loads(
            request.POST.get("state", ""), salt=SERVER_CONNECT_SALT, max_age=ACCOUNT_LINK_MAX_AGE_SECONDS
        )
    except signing.BadSignature:
        return JsonResponse({"error": "invalid or expired link"}, status=400)
    if data.get("user_id") != request.user.id:
        return JsonResponse({"error": "session mismatch"}, status=403)

    team_id_raw = request.POST.get("team_id", "")
    if not team_id_raw.isdigit():
        return JsonResponse({"error": "invalid project"}, status=400)
    team = next((t for t in _admin_teams(request.user) if t.id == int(team_id_raw)), None)
    if team is None:
        return JsonResponse({"error": "connecting requires org admin access to that project"}, status=403)

    integration, _created = Integration.objects.update_or_create(
        team=team,
        kind="discord",
        integration_id=data["guild_id"],
        defaults={"created_by": request.user, "config": {"guild_name": data.get("guild_name", "")}},
    )
    # Last connect wins for command routing — the bot has no project-picker command, so
    # `/ph connect` is the only routing control. Matches connect_guild's capture-key semantics.
    DiscordSettings.objects.update_or_create(
        guild_id=data["guild_id"],
        discord_user_id=None,
        defaults={"default_integration": integration},
    )

    # Provision analytics on the bot side — this push is what wires up guild capture.
    provision_error: str | None = None
    try:
        result = DiscordBotClient().connect_guild(
            guild_id=data["guild_id"],
            region=discord_deployment_region(),
            project_api_key=team.api_token,
        )
        if not result.get("ok"):
            provision_error = f"bot replied {result!r}"
    except Exception as e:
        provision_error = str(e)
    if provision_error:
        logger.warning("discord_connect_guild_provision_failed", guild_id=data["guild_id"], error=provision_error)

    guild_label = escape(data.get("guild_name") or data["guild_id"])
    message = (
        f"Discord server {guild_label} is now connected to project {escape(team.name)} (id {team.id}). "
        "You can close this tab and run /ph in Discord."
    )
    if provision_error:
        message += " Note: provisioning analytics on the bot failed — reconnect to retry."
    return HttpResponse(message)
