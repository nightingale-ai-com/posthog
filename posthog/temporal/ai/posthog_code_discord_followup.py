# Workflows in this module run on the max-ai temporal task queue.
import json
from dataclasses import dataclass
from datetime import timedelta

import structlog
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow

logger = structlog.get_logger(__name__)

POSTHOG_CODE_DISCORD_FOLLOWUP_TIMEOUT_SECONDS = 4 * 60

FOLLOWUP_TERMINAL_MESSAGE = "This task has finished — start a new one with `/ph code` in a channel."
FOLLOWUP_STARTING_MESSAGE = "The agent is still starting up. Give it a moment and try again."
FOLLOWUP_FAILED_MESSAGE = "I couldn't deliver that to the agent. Please try again."


@dataclass
class PostHogCodeDiscordFollowupInputs:
    """A follow-up message for a running task, sent from its Discord thread.

    Arrives either as `/ph code <text>` inside the task thread or (once the bot
    forwards them) as a plain message in that thread.
    """

    guild_id: str
    thread_id: str
    text: str
    discord_user_id: str
    message_id: str | None = None


@workflow.defn(name="posthog-code-discord-followup-processing")
class PostHogCodeDiscordFollowupWorkflow(PostHogWorkflow):
    @staticmethod
    def parse_inputs(inputs: list[str]) -> PostHogCodeDiscordFollowupInputs:
        loaded = json.loads(inputs[0])
        return PostHogCodeDiscordFollowupInputs(**loaded)

    @workflow.run
    async def run(self, inputs: PostHogCodeDiscordFollowupInputs) -> None:
        await workflow.execute_activity(
            forward_discord_followup_activity,
            inputs,
            start_to_close_timeout=timedelta(seconds=POSTHOG_CODE_DISCORD_FOLLOWUP_TIMEOUT_SECONDS),
            # send_user_message already retries once internally; activity-level retries
            # would double-deliver the message to the agent.
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@activity.defn
def forward_discord_followup_activity(inputs: PostHogCodeDiscordFollowupInputs) -> None:
    """Deliver a thread follow-up to the running sandbox agent. Mirrors the Slack
    follow-up path (minus cross-user identity resolution — the ingest layer already
    required an account-linked sender)."""
    from products.discord_app.backend.discord_thread import DiscordThreadContext, DiscordThreadHandler
    from products.discord_app.backend.models import DiscordThreadTaskMapping
    from products.tasks.backend.services.agent_command import send_user_message
    from products.tasks.backend.services.connection_token import create_sandbox_connection_token

    mapping = (
        DiscordThreadTaskMapping.objects.unscoped()
        .filter(guild_id=inputs.guild_id, thread_id=inputs.thread_id)
        .select_related("task_run", "task__created_by", "integration")
        .first()
    )
    if mapping is None:
        logger.warning("posthog_code_discord_followup_mapping_not_found", thread_id=inputs.thread_id)
        return

    handler = DiscordThreadHandler(
        DiscordThreadContext(
            integration_id=mapping.integration_id,
            channel_id=mapping.thread_id,
            thread_id=mapping.thread_id,
            anchor_message_id=mapping.anchor_message_id or None,
            discord_user_id=mapping.discord_user_id or None,
        )
    )
    task_run = mapping.task_run

    if task_run.is_terminal:
        handler.post_thread_message(FOLLOWUP_TERMINAL_MESSAGE)
        return

    if not (task_run.state or {}).get("sandbox_url"):
        handler.post_thread_message(FOLLOWUP_STARTING_MESSAGE)
        return

    text = (inputs.text or "").strip()
    if not text:
        return

    auth_token = None
    created_by = mapping.task.created_by
    if created_by and created_by.id:
        distinct_id = created_by.distinct_id or f"user_{created_by.id}"
        auth_token = create_sandbox_connection_token(task_run, user_id=created_by.id, distinct_id=distinct_id)

    result = send_user_message(task_run, text, auth_token=auth_token, timeout=90)
    if not result.success and result.retryable and result.status_code != 504:
        result = send_user_message(task_run, text, auth_token=auth_token, timeout=90)

    if result.success or (result.retryable and result.status_code == 504):
        # 504 means the agent is mid-turn; the relay posts its reply when it finishes.
        return

    logger.warning(
        "posthog_code_discord_followup_forwarding_failed",
        thread_id=inputs.thread_id,
        error=result.error,
        status_code=result.status_code,
    )
    handler.post_thread_message(FOLLOWUP_FAILED_MESSAGE)
