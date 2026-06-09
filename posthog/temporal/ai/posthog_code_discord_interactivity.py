import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow

POSTHOG_CODE_DISCORD_INTERACTIVITY_TIMEOUT_SECONDS = 5 * 60
logger = structlog.get_logger(__name__)

TERMINATE_CUSTOM_ID = "posthog_code_terminate_task"


@dataclass
class PostHogCodeDiscordInteractivityInputs:
    payload: dict[str, Any]


@workflow.defn(name="posthog-code-discord-terminate-task-processing")
class PostHogCodeDiscordTerminateTaskWorkflow(PostHogWorkflow):
    @staticmethod
    def parse_inputs(inputs: list[str]) -> PostHogCodeDiscordInteractivityInputs:
        loaded = json.loads(inputs[0])
        return PostHogCodeDiscordInteractivityInputs(**loaded)

    @workflow.run
    async def run(self, inputs: PostHogCodeDiscordInteractivityInputs) -> None:
        await workflow.execute_activity(
            process_posthog_code_discord_terminate_task_activity,
            args=(inputs,),
            start_to_close_timeout=timedelta(seconds=POSTHOG_CODE_DISCORD_INTERACTIVITY_TIMEOUT_SECONDS),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


@activity.defn
def process_posthog_code_discord_terminate_task_activity(inputs: PostHogCodeDiscordInteractivityInputs) -> None:
    process_posthog_code_discord_task_termination_payload(inputs.payload)


def process_posthog_code_discord_task_termination_payload(payload: dict[str, Any]) -> None:
    """Cancel a running task from a Discord "Stop task" button.

    The button ``custom_id`` is ``posthog_code_terminate_task:{run_id}``. Authorization is
    by the thread mapping (only a participant in the same guild can press it).
    """
    import asyncio

    from posthog.temporal.common.client import sync_connect

    from products.discord_app.backend.models import DiscordThreadTaskMapping
    from products.tasks.backend.models import TaskRun
    from products.tasks.backend.services.agent_command import send_cancel
    from products.tasks.backend.services.connection_token import create_sandbox_connection_token
    from products.tasks.backend.temporal.process_task.workflow import ProcessTaskWorkflow

    custom_id = payload.get("custom_id", "")
    guild_id = payload.get("guild_id")
    _, _, run_id = custom_id.partition(":")
    if not run_id or not guild_id:
        logger.warning("posthog_code_discord_terminate_missing_context", custom_id=custom_id)
        return

    mapping = (
        DiscordThreadTaskMapping.objects.unscoped()
        .filter(guild_id=guild_id, task_run_id=run_id)
        .select_related("task_run__task")
        .first()
    )
    if mapping is None:
        logger.warning("posthog_code_discord_terminate_mapping_not_found", run_id=run_id)
        return

    try:
        task_run = TaskRun.objects.select_related("task").get(id=run_id, team_id=mapping.team_id)
    except TaskRun.DoesNotExist:
        logger.warning("posthog_code_discord_terminate_run_not_found", run_id=run_id)
        return

    if task_run.is_terminal:
        logger.info("posthog_code_discord_terminate_already_terminal", run_id=run_id, status=task_run.status)
        return

    auth_token = None
    created_by = task_run.task.created_by
    if created_by and isinstance(created_by.id, int):
        distinct_id = created_by.distinct_id or f"user_{created_by.id}"
        try:
            auth_token = create_sandbox_connection_token(task_run, user_id=created_by.id, distinct_id=distinct_id)
        except Exception as e:
            logger.warning("posthog_code_discord_terminate_auth_token_failed", run_id=run_id, error=str(e))

    cancel_result = send_cancel(task_run, auth_token=auth_token)
    if not cancel_result.success:
        logger.warning("posthog_code_discord_terminate_command_failed", run_id=run_id, error=cancel_result.error)

    try:
        client = sync_connect()
        handle = client.get_workflow_handle(task_run.workflow_id)
        asyncio.run(handle.signal(ProcessTaskWorkflow.complete_task, args=["cancelled", "Run terminated from Discord"]))
        logger.info("posthog_code_discord_terminate_signaled", run_id=run_id)
    except Exception as e:
        logger.exception("posthog_code_discord_terminate_signal_failed", run_id=run_id, error=str(e))
