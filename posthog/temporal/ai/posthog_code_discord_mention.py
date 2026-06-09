# Workflows in this module run on the max-ai temporal task queue.
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import structlog
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow

logger = structlog.get_logger(__name__)

POSTHOG_CODE_DISCORD_MENTION_TIMEOUT_SECONDS = 10 * 60
POSTHOG_CODE_DISCORD_PICKER_TIMEOUT_MINUTES = 15

# Emoji the bot reacts with (passed through verbatim to the Discord REST API).
_EYES = "\U0001f440"

REPO_SELECT_CUSTOM_ID = "posthog_code_repo_select"
REPO_NONE_CUSTOM_ID = "posthog_code_repo_none"


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
                workflow_id = workflow.info().workflow_id
                await workflow.execute_activity(
                    post_discord_repo_picker_activity,
                    args=(inputs, thread_id, workflow_id),
                    start_to_close_timeout=timeout,
                    retry_policy=retry,
                )
                try:
                    await workflow.wait_condition(
                        lambda: self._repo_selection_resolved,
                        timeout=timedelta(minutes=POSTHOG_CODE_DISCORD_PICKER_TIMEOUT_MINUTES),
                    )
                except TimeoutError:
                    self._repo_selection_resolved = True
                repository = self._selected_repo

            await workflow.execute_activity(
                create_discord_task_activity,
                args=(inputs, repository, anchor_message_id, thread_id),
                start_to_close_timeout=timeout,
                retry_policy=retry,
            )
        except Exception:
            logger.exception("posthog_code_discord_mention_failed", guild_id=inputs.guild_id)


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
    channel_id = inputs.interaction.get("channel_id")
    prompt = (inputs.interaction.get("options") or {}).get("prompt") or "Task from Discord"
    title = prompt.strip()[:80] or "PostHog Code task"

    thread = client.create_thread(channel_id=channel_id, name=title, message_id=inputs.interaction.get("message_id"))
    thread_id = thread.get("thread_id") or channel_id

    anchor = client.post_message(target_id=thread_id, content="**PostHog Code is on it…** :hourglass_flowing_sand:")
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
def post_discord_repo_picker_activity(
    inputs: PostHogCodeDiscordMentionWorkflowInputs, thread_id: str, workflow_id: str
) -> None:
    """Post a repo string-select (≤25) + "No repo needed" button into the thread.

    The custom_id carries the workflow id so the ingress can signal this workflow when the
    user picks. Discord string selects cap at 25 options (no live typeahead) — autocomplete
    on the ``repo:`` command option covers larger orgs.
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
            content="Which repository should I work in?",
            components=components,
        )
    except Exception:
        logger.exception("posthog_code_discord_repo_picker_post_failed")


@activity.defn
def create_discord_task_activity(
    inputs: PostHogCodeDiscordMentionWorkflowInputs,
    repository: str | None,
    anchor_message_id: str,
    thread_id: str,
) -> None:
    """Create the Task + TaskRun + thread mapping, then start the process workflow."""
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
        logger.exception("posthog_code_discord_task_creation_failed", team_id=integration.team_id)
        return

    task_run = task.latest_run if task else None
    if not task or not task_run:
        return

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
