from dataclasses import dataclass
from typing import Any

from django.conf import settings

from temporalio import activity

from posthog.models.user import User
from posthog.temporal.common.logger import get_logger
from posthog.temporal.common.utils import close_db_connections

from products.tasks.backend.access import has_tasks_access

logger = get_logger(__name__)


@dataclass
class PostDiscordUpdateInput:
    run_id: str
    discord_thread_context: dict[str, Any]
    sandbox_cleaned: bool = False


def _viewer_has_posthog_code_access(viewer: User | None) -> bool:
    if viewer is None:
        return False
    try:
        return has_tasks_access(viewer)
    except Exception:
        logger.exception("post_discord_update_access_check_failed", user_id=getattr(viewer, "id", None))
        return False


@activity.defn
@close_db_connections
def post_discord_update(input: PostDiscordUpdateInput) -> None:
    """Post a Discord update based on current task run state. Idempotent.

    Mirror of ``post_slack_update`` for the Discord bridge — same branching, but driven
    through ``DiscordThreadHandler`` (which calls the companion bot's actions API).
    """
    from products.discord_app.backend.discord_thread import DiscordThreadContext, DiscordThreadHandler
    from products.tasks.backend.models import TaskRun

    try:
        task_run = TaskRun.objects.select_related("task", "task__created_by").get(id=input.run_id)
    except TaskRun.DoesNotExist:
        logger.warning("post_discord_update_task_run_not_found", run_id=input.run_id)
        return

    if task_run.is_terminal:
        # The conversation is over — deregister the thread from the bot's reply
        # forwarding. Idempotent on the bot side; fired before the final status post
        # since ordering doesn't matter (it gates forwarding, not posting).
        _unwatch_thread_for_run(task_run)

    try:
        context = DiscordThreadContext.from_dict(input.discord_thread_context)
        handler = DiscordThreadHandler(context)
        creator_has_access = _viewer_has_posthog_code_access(task_run.task.created_by)
        task_url: str | None = (
            f"{settings.SITE_URL}/project/{task_run.task.team_id}/tasks/{task_run.task_id}?runId={task_run.id}"
            if creator_has_access
            else None
        )
        pr_url = (task_run.output or {}).get("pr_url")

        # Settle the channel's deferred-interaction placeholder once the run is over;
        # progress edits keep it live mid-run, but a terminal state must not leave
        # "posthog is thinking…" dangling.
        if task_run.is_terminal:
            handler.finalize_placeholder(_placeholder_text_for(task_run, pr_url))

        if input.sandbox_cleaned:
            if pr_url:
                handler.update_reaction("hedgehog")
                if _is_pr_opened_notified(task_run, pr_url):
                    handler.delete_progress()
                    return
                handler.post_pr_opened_sandbox_cleaned(pr_url, task_url)
                _mark_pr_opened_notified(task_run, pr_url)
            elif task_run.status == TaskRun.Status.CANCELLED:
                handler.update_reaction("hedgehog")
                handler.post_cancelled(task_url)
            elif task_run.status == TaskRun.Status.FAILED:
                error = task_run.error_message or "Unknown error"
                handler.update_reaction("x")
                handler.post_error(error, task_url)
            return

        if task_run.status == TaskRun.Status.COMPLETED:
            handler.update_reaction("hedgehog")
            if task_run.error_message and "timed out" in task_run.error_message:
                handler.delete_progress()
                return
            handler.post_completion(pr_url, task_url)
        elif task_run.status == TaskRun.Status.CANCELLED:
            handler.update_reaction("hedgehog")
            handler.post_cancelled(task_url)
        elif task_run.status == TaskRun.Status.FAILED:
            error = task_run.error_message or "Unknown error"
            handler.update_reaction("x")
            handler.post_error(error, task_url)
        else:
            if pr_url:
                _post_pr_opened_notification_once(task_run, handler, pr_url, task_url)
                handler.update_reaction("eyes")
                handler.delete_progress()
                return
            stage = _get_stage_from_status(task_run.status, task_run.stage)
            # Discord link buttons only allow http(s)/discord schemes, so the progress
            # button gets the web task URL, not the posthog-code:// desktop deeplink.
            handler.post_or_update_progress(stage, task_url)
    except Exception:
        logger.exception("post_discord_update_failed", run_id=input.run_id)


def _placeholder_text_for(task_run, pr_url: str | None) -> str:
    from products.tasks.backend.models import TaskRun

    if task_run.status == TaskRun.Status.COMPLETED:
        return "**Pull request created** 🚀 — see the thread." if pr_url else "**Task completed** 🦔 — see the thread."
    if task_run.status == TaskRun.Status.CANCELLED:
        return "**Task cancelled** 🦔"
    return "**Task failed** ❌ — details in the thread."


def _unwatch_thread_for_run(task_run) -> None:
    from posthog.models.integration import DiscordBotClient, discord_bridge_configured

    from products.discord_app.backend.models import DiscordThreadTaskMapping

    if not discord_bridge_configured():
        return
    mapping = DiscordThreadTaskMapping.objects.unscoped().filter(task_run=task_run).first()
    if mapping is None:
        return
    try:
        DiscordBotClient().unwatch_thread(guild_id=mapping.guild_id, thread_id=mapping.thread_id)
    except Exception as e:
        logger.warning("post_discord_update_unwatch_failed", run_id=str(task_run.id), error=str(e))


def _get_stage_from_status(status: str, stage: str | None = None) -> str:
    if stage:
        return stage

    from products.tasks.backend.models import TaskRun

    status_map: dict[str, str] = {
        TaskRun.Status.NOT_STARTED: "Starting up...",
        TaskRun.Status.QUEUED: "Queued...",
        TaskRun.Status.IN_PROGRESS: "In progress...",
    }
    return status_map.get(status, "In progress...")


def _post_pr_opened_notification_once(task_run, handler, pr_url: str, task_url: str | None) -> None:
    if _is_pr_opened_notified(task_run, pr_url):
        return
    handler.post_pr_opened(pr_url, task_url)
    _mark_pr_opened_notified(task_run, pr_url)


def _is_pr_opened_notified(task_run, pr_url: str) -> bool:
    state = task_run.state or {}
    if not state.get("discord_pr_opened_notified"):
        return False
    notified_url = state.get("discord_notified_pr_url")
    return notified_url == pr_url if notified_url else True


def _mark_pr_opened_notified(task_run, pr_url: str) -> None:
    from products.tasks.backend.models import TaskRun

    TaskRun.update_state_atomic(
        task_run.id,
        updates={
            "discord_pr_opened_notified": True,
            "discord_notified_pr_url": pr_url,
        },
    )
