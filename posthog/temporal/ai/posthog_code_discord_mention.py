# Workflows in this module run on the max-ai temporal task queue.
import json
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import structlog
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow
from posthog.temporal.common.heartbeat import Heartbeater

logger = structlog.get_logger(__name__)

POSTHOG_CODE_DISCORD_MENTION_TIMEOUT_SECONDS = 10 * 60
POSTHOG_CODE_DISCORD_PICKER_TIMEOUT_MINUTES = 15

# Emoji the bot reacts with (passed through verbatim to the Discord REST API).
_EYES = "\U0001f440"

REPO_SELECT_CUSTOM_ID = "posthog_code_repo_select"
REPO_NONE_CUSTOM_ID = "posthog_code_repo_none"

INTERNAL_ERROR_MESSAGE = "**Something went wrong** ❌\nI ran into an internal error and couldn't start the task. Please try `/ph code` again."
CHANNEL_ACCESS_ERROR_MESSAGE = (
    "**I can't access this channel** ❌\n"
    "Discord refused the bot (Missing Access). Make sure the PostHog bot can see this channel "
    "(View channel + Create public threads + Send messages in threads — for private channels, add the bot "
    "explicitly), then try `/ph code` again."
)


def _failure_message_for(exc: BaseException) -> str:
    """Pick the user-facing failure message by walking the activity error's cause chain.

    Discord's 50001 Missing Access (bot can't see the channel) is an actionable user error,
    not an internal one — surface instructions instead of a generic apology.
    """
    seen: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < 10:
        seen.append(str(current))
        current = current.__cause__ or current.__context__
    blob = " ".join(seen)
    if "Missing Access" in blob or "50001" in blob:
        return CHANNEL_ACCESS_ERROR_MESSAGE
    return INTERNAL_ERROR_MESSAGE


PICKER_TIMEOUT_MESSAGE = (
    f"**No repository selected** — I waited {POSTHOG_CODE_DISCORD_PICKER_TIMEOUT_MINUTES} minutes, "
    "so this task wasn't started. Run `/ph code` again when you're ready."
)


@dataclass
class PostHogCodeDiscordMentionWorkflowInputs:
    """Inputs for a Discord ``/posthog`` invocation, forwarded by the companion bot.

    ``user_id`` is the PostHog user resolved from ``DiscordUserLink`` at ingest time
    (Discord exposes no email, so identity is established by account-linking, not here).
    """

    interaction: dict[str, Any]
    integration_id: int
    guild_id: str
    user_id: int
    discord_user_id: str


def derive_discord_mention_workflow_id(inputs: "PostHogCodeDiscordMentionWorkflowInputs") -> str:
    """Deterministic dispatch id, keyed on the Discord interaction id for idempotency."""
    suffix = inputs.interaction.get("interaction_id") or inputs.interaction.get("channel_id", "")
    return f"posthog-code-discord-mention-{inputs.guild_id}:{suffix}"


@dataclass
class DiscordRepoResolution:
    mode: Literal["auto", "no_repo", "needs_picker"]
    repository: str | None


@workflow.defn(name="posthog-code-discord-mention-processing")
class PostHogCodeDiscordMentionWorkflow(PostHogWorkflow):
    def __init__(self) -> None:
        self._selected_repo: str | None = None
        self._repo_selection_resolved = False

    @workflow.signal
    async def repo_selected(self, repository: str) -> None:
        if not self._repo_selection_resolved:
            self._repo_selection_resolved = True
            self._selected_repo = repository

    @workflow.signal
    async def no_repo_needed(self) -> None:
        if not self._repo_selection_resolved:
            self._repo_selection_resolved = True
            self._selected_repo = None

    @staticmethod
    def parse_inputs(inputs: list[str]) -> PostHogCodeDiscordMentionWorkflowInputs:
        loaded = json.loads(inputs[0])
        return PostHogCodeDiscordMentionWorkflowInputs(**loaded)

    @workflow.run
    async def run(self, inputs: PostHogCodeDiscordMentionWorkflowInputs) -> None:
        timeout = timedelta(seconds=POSTHOG_CODE_DISCORD_MENTION_TIMEOUT_SECONDS)
        retry = RetryPolicy(maximum_attempts=2)
        anchor_message_id: str | None = None
        thread_id: str | None = None

        try:
            blocked = await workflow.execute_activity(
                enforce_discord_billing_quota_activity,
                inputs,
                start_to_close_timeout=timeout,
                retry_policy=retry,
            )
            if blocked:
                return

            thread = await workflow.execute_activity(
                prepare_discord_thread_activity,
                inputs,
                start_to_close_timeout=timeout,
                retry_policy=retry,
            )
            anchor_message_id = thread["anchor_message_id"]
            thread_id = thread["thread_id"]

            resolution: dict[str, Any] = await workflow.execute_activity(
                resolve_discord_repository_activity,
                inputs,
                start_to_close_timeout=timeout,
                retry_policy=retry,
            )

            repository: str | None = resolution.get("repository")
            if resolution.get("mode") == "needs_picker":
                # Mirror the Slack cascade: Haiku gate ("does this need a repo at all?"),
                # then the discovery agent, with the interactive picker as the failure fallback.
                needs_repo = await workflow.execute_activity(
                    classify_discord_task_needs_repo_activity,
                    inputs,
                    start_to_close_timeout=timeout,
                    retry_policy=retry,
                )
                if not needs_repo:
                    repository = None
                else:
                    outcome: dict[str, Any] = await workflow.execute_activity(
                        discover_discord_repository_via_agent_activity,
                        args=(inputs, thread_id, anchor_message_id),
                        start_to_close_timeout=timeout,
                        retry_policy=RetryPolicy(maximum_attempts=1),
                        heartbeat_timeout=timedelta(minutes=2),
                    )
                    if outcome.get("status") == "found":
                        repository = outcome.get("repository")
                    elif outcome.get("status") == "no_match":
                        repository = None
                    else:
                        repository = await self._pick_repository_interactively(
                            inputs, thread_id, anchor_message_id, outcome.get("reason")
                        )
                        if not self._repo_selection_resolved:
                            return

            await workflow.execute_activity(
                create_discord_task_activity,
                args=(inputs, repository, anchor_message_id, thread_id),
                start_to_close_timeout=timeout,
                retry_policy=retry,
            )
        except Exception as exc:
            workflow.logger.exception(
                "posthog_code_discord_mention_failed",
                extra={"guild_id": inputs.guild_id, "error": str(exc), "error_type": type(exc).__name__},
            )
            await self._notify_failure(inputs, thread_id, anchor_message_id, _failure_message_for(exc))

    async def _pick_repository_interactively(
        self,
        inputs: PostHogCodeDiscordMentionWorkflowInputs,
        thread_id: str,
        anchor_message_id: str,
        note: str | None,
    ) -> str | None:
        """Post the repo picker and wait for the selection signal.

        On timeout, notifies the user and returns with ``_repo_selection_resolved`` still
        False — callers use that to abort instead of starting a task nobody asked for.
        """
        await workflow.execute_activity(
            post_discord_repo_picker_activity,
            args=(inputs, thread_id, workflow.info().workflow_id, note),
            start_to_close_timeout=timedelta(seconds=POSTHOG_CODE_DISCORD_MENTION_TIMEOUT_SECONDS),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        try:
            await workflow.wait_condition(
                lambda: self._repo_selection_resolved,
                timeout=timedelta(minutes=POSTHOG_CODE_DISCORD_PICKER_TIMEOUT_MINUTES),
            )
        except TimeoutError:
            await self._notify_failure(inputs, thread_id, anchor_message_id, PICKER_TIMEOUT_MESSAGE)
            return None
        return self._selected_repo

    async def _notify_failure(
        self,
        inputs: PostHogCodeDiscordMentionWorkflowInputs,
        thread_id: str | None,
        anchor_message_id: str | None,
        message: str,
    ) -> None:
        """Best-effort: never let the anchor sit on "PostHog Code is on it…" after a failure."""
        try:
            await workflow.execute_activity(
                post_discord_workflow_failure_activity,
                args=(inputs, thread_id, anchor_message_id, message),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:
            workflow.logger.exception("posthog_code_discord_failure_notice_failed")


def _get_integration_and_client(integration_id: int) -> tuple[Any, Any]:
    from posthog.models.integration import DiscordIntegration, Integration

    integration = Integration.objects.get(id=integration_id, kind="discord")
    return integration, DiscordIntegration(integration).client


@activity.defn
def enforce_discord_billing_quota_activity(inputs: PostHogCodeDiscordMentionWorkflowInputs) -> bool:
    """Refuse the turn when the team is over its AI-credits quota. Returns True if blocked."""
    from ee.billing.quota_limiting import QuotaLimitingCaches, QuotaResource, is_team_limited

    try:
        integration, client = _get_integration_and_client(inputs.integration_id)
    except Exception:
        logger.exception("posthog_code_discord_quota_integration_missing", integration_id=inputs.integration_id)
        return True

    if not is_team_limited(
        integration.team.api_token,
        QuotaResource.AI_CREDITS,
        QuotaLimitingCaches.QUOTA_LIMITER_CACHE_KEY,
    ):
        return False

    token = inputs.interaction.get("interaction_token")
    try:
        client.post_message(
            target_id=inputs.interaction.get("channel_id"),
            content="This team is out of AI credits, so I can't start a task right now.",
            ephemeral=True,
            interaction_token=token,
        )
    except Exception:
        logger.warning("posthog_code_discord_quota_denial_post_failed")
    return True


@activity.defn
def prepare_discord_thread_activity(inputs: PostHogCodeDiscordMentionWorkflowInputs) -> dict[str, str]:
    """Create the thread, post the anchor message, and react with 👀.

    The anchor lives inside the thread, so its ``channel_id`` for later reactions/edits is
    the thread id. Returns the anchor message id and thread id.
    """
    _integration, client = _get_integration_and_client(inputs.integration_id)
    channel_id = str(inputs.interaction.get("channel_id") or "")
    prompt = (inputs.interaction.get("options") or {}).get("prompt") or "Task from Discord"
    title = prompt.strip()[:80] or "PostHog Code task"

    thread = client.create_thread(channel_id=channel_id, name=title, message_id=inputs.interaction.get("message_id"))
    thread_id = str(thread.get("thread_id") or "") or channel_id

    # Register the thread so the bot forwards replies in it back as kind=message.
    # Best-effort: a failure only disables conversational follow-ups, not the task.
    if thread.get("thread_id"):
        try:
            client.watch_thread(guild_id=inputs.guild_id, thread_id=thread_id)
        except Exception:
            logger.warning("posthog_code_discord_watch_thread_failed", thread_id=thread_id)

    # Discord renders :shortcodes: in API-sent content literally, so use unicode emoji.
    anchor = client.post_message(target_id=thread_id, content="**PostHog Code is on it…** ⏳")
    anchor_message_id = anchor.get("message_id", "")

    if anchor_message_id:
        try:
            client.add_reaction(thread_id, anchor_message_id, _EYES)
        except Exception:
            logger.warning("posthog_code_discord_anchor_reaction_failed")

    return {"anchor_message_id": anchor_message_id, "thread_id": thread_id}


@activity.defn
def resolve_discord_repository_activity(inputs: PostHogCodeDiscordMentionWorkflowInputs) -> dict[str, Any]:
    """Fast-path repo resolution: explicit ``repo`` option, sole repo, none, or picker.

    The LLM repo-discovery agent (reused from the Slack bridge) can be layered in later;
    this covers the deterministic cases.
    """
    from products.discord_app.backend.repos import get_full_repo_names

    integration, _client = _get_integration_and_client(inputs.integration_id)
    options = inputs.interaction.get("options") or {}
    explicit = (options.get("repo") or "").strip()
    repos = get_full_repo_names(integration, user_id=inputs.user_id)

    if explicit:
        match = next((r for r in repos if r.lower() == explicit.lower()), explicit)
        return {"mode": "auto", "repository": match}
    if len(repos) == 1:
        return {"mode": "auto", "repository": repos[0]}
    if not repos:
        return {"mode": "no_repo", "repository": None}
    return {"mode": "needs_picker", "repository": None}


@activity.defn
def classify_discord_task_needs_repo_activity(inputs: PostHogCodeDiscordMentionWorkflowInputs) -> bool:
    """Haiku gate: does this prompt need repository access at all?

    Slash commands carry no thread history at invocation, so the classifier runs on the
    prompt alone. Defaults to True on error (shared classifier is conservative).
    """
    from products.tasks.backend.repo_selection.classifier import classify_task_needs_repo

    prompt = (inputs.interaction.get("options") or {}).get("prompt") or ""
    return classify_task_needs_repo(prompt, [], product="posthog_code")


@activity.defn
async def discover_discord_repository_via_agent_activity(
    inputs: PostHogCodeDiscordMentionWorkflowInputs, thread_id: str, anchor_message_id: str
) -> dict[str, Any]:
    """Run the shared repo discovery agent over the /ph prompt.

    Returns ``{"status": "found"|"no_match"|"failed", "repository", "reason"}`` — all
    exceptions are caught and surfaced as ``failed`` so the workflow falls back to the
    interactive picker. Mirrors the Slack bridge's wrapper.
    """
    from products.tasks.backend.models import Task
    from products.tasks.backend.repo_selection.agent import (
        RepoSelectionRejectedError,
        RepoSelectionUnavailableError,
        select_repository,
    )

    integration, client = await asyncio.to_thread(_get_integration_and_client, inputs.integration_id)

    # The agent takes 10–60s; repurpose the anchor as a progress indicator meanwhile.
    if anchor_message_id:
        try:
            await asyncio.to_thread(
                client.edit_message,
                target_id=thread_id,
                message_id=anchor_message_id,
                content="**Finding the right repository…** 🔍",
            )
        except Exception:
            logger.warning("posthog_code_discord_discovery_progress_edit_failed")

    prompt = (inputs.interaction.get("options") or {}).get("prompt") or ""
    try:
        async with Heartbeater():
            result = await select_repository(
                team_id=integration.team_id,
                user_id=inputs.user_id,
                context=prompt,
                origin_product=Task.OriginProduct.DISCORD,
            )
    except RepoSelectionRejectedError:
        # Don't echo the returned repository — it's raw LLM output and reaches Discord.
        return {"status": "failed", "repository": None, "reason": "Agent returned an unrecognized repository."}
    except RepoSelectionUnavailableError as exc:
        return {"status": "failed", "repository": None, "reason": f"Repo selection unavailable: {exc.reason}"}
    except Exception as exc:
        logger.exception("posthog_code_discord_repo_discovery_failed", error=str(exc))
        return {"status": "failed", "repository": None, "reason": f"Agent failed: {type(exc).__name__}"}

    if result.repository is None:
        return {"status": "no_match", "repository": None, "reason": result.reason}
    return {"status": "found", "repository": result.repository, "reason": result.reason}


@activity.defn
def post_discord_repo_picker_activity(
    inputs: PostHogCodeDiscordMentionWorkflowInputs, thread_id: str, workflow_id: str, note: str | None = None
) -> None:
    """Post a repo string-select (≤25) + "No repo needed" button into the thread.

    The custom_id carries the workflow id so the ingress can signal this workflow when the
    user picks. Discord string selects cap at 25 options (no live typeahead) — autocomplete
    on the ``repo:`` command option covers larger orgs. ``note`` (e.g. why the discovery
    agent fell back) is shown italicized above the prompt.
    """
    from products.discord_app.backend.repos import MAX_AUTOCOMPLETE_CHOICES, get_full_repo_names

    integration, client = _get_integration_and_client(inputs.integration_id)
    repos = get_full_repo_names(integration, user_id=inputs.user_id)[:MAX_AUTOCOMPLETE_CHOICES]
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 3,  # string select
                    "custom_id": f"{REPO_SELECT_CUSTOM_ID}:{workflow_id}",
                    "placeholder": "Select a repository",
                    "options": [{"label": r[:100], "value": r[:100]} for r in repos],
                }
            ],
        },
        {
            "type": 1,
            "components": [
                {
                    "type": 2,  # button
                    "style": 2,
                    "label": "No repo needed",
                    "custom_id": f"{REPO_NONE_CUSTOM_ID}:{workflow_id}",
                }
            ],
        },
    ]
    try:
        client.post_message(
            target_id=thread_id,
            content=f"*{note}*\nWhich repository should I work in?" if note else "Which repository should I work in?",
            components=components,
        )
    except Exception:
        logger.exception("posthog_code_discord_repo_picker_post_failed")


@activity.defn
def post_discord_workflow_failure_activity(
    inputs: PostHogCodeDiscordMentionWorkflowInputs,
    thread_id: str | None,
    anchor_message_id: str | None,
    message: str,
) -> None:
    """Surface a failure to the user: edit the anchor in place when it exists, else post in
    the channel. Either way, settle the deferred-interaction placeholder (token-only edits
    target ``@original``) so "posthog is thinking…" never lingers past a failure."""
    _integration, client = _get_integration_and_client(inputs.integration_id)
    token = inputs.interaction.get("interaction_token")
    if token:
        try:
            client.edit_message(content=message, interaction_token=token)
        except Exception:
            logger.warning("posthog_code_discord_placeholder_edit_failed")
    if thread_id and anchor_message_id:
        client.edit_message(target_id=thread_id, message_id=anchor_message_id, content=message)
    elif not token:
        client.post_message(target_id=thread_id or inputs.interaction.get("channel_id"), content=message)


@activity.defn
def create_discord_task_activity(
    inputs: PostHogCodeDiscordMentionWorkflowInputs,
    repository: str | None,
    anchor_message_id: str,
    thread_id: str,
) -> None:
    """Create the Task + TaskRun + thread mapping, then start the process workflow."""
    from posthog.models.scoping import team_scope

    from products.discord_app.backend.discord_thread import DiscordThreadContext
    from products.discord_app.backend.models import DiscordThreadTaskMapping
    from products.tasks.backend.models import Task, TaskRun
    from products.tasks.backend.temporal.client import execute_task_processing_workflow

    integration, _client = _get_integration_and_client(inputs.integration_id)
    options = inputs.interaction.get("options") or {}
    prompt = (options.get("prompt") or "Task from Discord").strip()
    title = prompt[:80] or "PostHog Code task"

    thread_context = DiscordThreadContext(
        integration_id=integration.id,
        # The anchor lives in the thread, so reactions/edits target the thread id.
        channel_id=thread_id,
        thread_id=thread_id,
        anchor_message_id=anchor_message_id,
        interaction_token=inputs.interaction.get("interaction_token"),
        discord_user_id=inputs.discord_user_id,
    )

    try:
        task = Task.create_and_run(
            team=integration.team,
            title=title,
            description=prompt,
            origin_product=Task.OriginProduct.DISCORD,
            user_id=inputs.user_id,
            repository=repository,
            create_pr=True,
            mode="interactive",
            start_workflow=False,
            posthog_mcp_scopes="full",
            initial_permission_mode="bypassPermissions",
        )
    except Exception:
        # Re-raise so the workflow's failure handler notifies the user — swallowing here
        # leaves the anchor stuck on a progress message forever.
        logger.exception("posthog_code_discord_task_creation_failed", team_id=integration.team_id)
        raise

    task_run = task.latest_run if task else None
    if not task or not task_run:
        raise RuntimeError(f"Task {task.id if task else '?'} was created without a run")

    # DiscordThreadTaskMapping is fail-closed team-scoped; this is a same-team write.
    with team_scope(integration.team_id):
        DiscordThreadTaskMapping.objects.update_or_create(
            integration=integration,
            channel_id=thread_id,
            thread_id=thread_id,
            defaults={
                "team": integration.team,
                "guild_id": inputs.guild_id,
                "anchor_message_id": anchor_message_id,
                "task": task,
                "task_run": task_run,
                "discord_user_id": inputs.discord_user_id,
            },
        )

    try:
        TaskRun.update_state_atomic(
            task_run.id,
            updates={"discord_mention_workflow_id": derive_discord_mention_workflow_id(inputs)},
        )
    except Exception:
        logger.exception("posthog_code_discord_persist_workflow_id_failed", task_run_id=str(task_run.id))

    execute_task_processing_workflow(
        task_id=str(task.id),
        run_id=str(task_run.id),
        team_id=task.team.id,
        user_id=inputs.user_id,
        create_pr=True,
        discord_thread_context=thread_context,
        posthog_mcp_scopes="full",
    )
