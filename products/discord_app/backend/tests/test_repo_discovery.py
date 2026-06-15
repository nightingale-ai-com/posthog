import asyncio
from contextlib import asynccontextmanager

import pytest
from unittest.mock import MagicMock, patch

from posthog.temporal.ai.posthog_code_discord_mention import (
    PostHogCodeDiscordMentionWorkflowInputs,
    classify_discord_task_needs_repo_activity,
    discover_discord_repository_via_agent_activity,
    post_discord_repo_picker_activity,
)

from products.tasks.backend.repo_selection.agent import (
    RepoSelectionRejectedError,
    RepoSelectionResult,
    RepoSelectionUnavailableError,
)


def _inputs(prompt: str = "fix the checkout bug") -> PostHogCodeDiscordMentionWorkflowInputs:
    return PostHogCodeDiscordMentionWorkflowInputs(
        interaction={"channel_id": "c1", "options": {"prompt": prompt}},
        integration_id=1,
        guild_id="g1",
        user_id=42,
        discord_user_id="du1",
    )


@asynccontextmanager
async def _noop_heartbeater():
    yield


class TestDiscordRepoCascade:
    def test_classify_runs_on_prompt_with_posthog_code_product(self):
        with patch(
            "products.tasks.backend.repo_selection.classifier.classify_task_needs_repo", return_value=False
        ) as classify:
            assert classify_discord_task_needs_repo_activity(_inputs("how many users churned?")) is False
        classify.assert_called_once_with("how many users churned?", [], product="posthog_code")

    def _run_discover(self, select_result=None, select_error=None):
        integration = MagicMock(team_id=7)
        client = MagicMock()

        async def fake_select(**kwargs):
            if select_error is not None:
                raise select_error
            return select_result

        with (
            patch(
                "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
                return_value=(integration, client),
            ),
            patch("posthog.temporal.ai.posthog_code_discord_mention.Heartbeater", _noop_heartbeater),
            patch("products.tasks.backend.repo_selection.agent.select_repository", side_effect=fake_select) as sel,
        ):
            outcome = asyncio.run(discover_discord_repository_via_agent_activity(_inputs(), "t1", "anchor1"))
        return outcome, client, sel

    def test_discover_found(self):
        result = RepoSelectionResult(repository="acme/web", reason="matches checkout code")
        outcome, client, sel = self._run_discover(select_result=result)
        assert outcome == {"status": "found", "repository": "acme/web", "reason": "matches checkout code"}
        # anchor repurposed as progress indicator while the agent runs
        client.edit_message.assert_called_once()
        assert "Finding the right repository" in client.edit_message.call_args.kwargs["content"]
        assert sel.call_args.kwargs["context"] == "fix the checkout bug"
        assert sel.call_args.kwargs["team_id"] == 7
        assert sel.call_args.kwargs["user_id"] == 42

    def test_discover_no_match(self):
        result = RepoSelectionResult(repository=None, reason="nothing plausible")
        outcome, _, _ = self._run_discover(select_result=result)
        assert outcome == {"status": "no_match", "repository": None, "reason": "nothing plausible"}

    @pytest.mark.parametrize(
        "error,reason_contains",
        [
            (RepoSelectionRejectedError("not/areal-repo", "made it up"), "unrecognized repository"),
            (RepoSelectionUnavailableError("all archived"), "all archived"),
            (RuntimeError("sandbox died"), "RuntimeError"),
        ],
    )
    def test_discover_failures_fall_back_to_picker(self, error, reason_contains):
        outcome, _, _ = self._run_discover(select_error=error)
        assert outcome["status"] == "failed"
        assert outcome["repository"] is None
        assert reason_contains in outcome["reason"]

    def test_rejected_error_does_not_echo_hallucinated_repo(self):
        outcome, _, _ = self._run_discover(select_error=RepoSelectionRejectedError("evil/__injection__", "reason"))
        assert "evil/__injection__" not in outcome["reason"]

    @pytest.mark.parametrize(
        "note,expected_prefix",
        [(None, "Which repository"), ("Agent failed: RuntimeError", "*Agent failed: RuntimeError*\nWhich repository")],
    )
    def test_picker_prepends_failure_note(self, note, expected_prefix):
        integration = MagicMock()
        client = MagicMock()
        with (
            patch(
                "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
                return_value=(integration, client),
            ),
            patch(
                "products.discord_app.backend.repos.get_full_repo_names",
                return_value=["acme/web", "acme/api"],
            ),
        ):
            post_discord_repo_picker_activity(_inputs(), "t1", "wf-1", note)
        assert client.post_message.call_args.kwargs["content"].startswith(expected_prefix)


class TestFailureMessages:
    def test_missing_access_in_cause_chain_gets_channel_message(self):
        from posthog.temporal.ai.posthog_code_discord_mention import (
            CHANNEL_ACCESS_ERROR_MESSAGE,
            INTERNAL_ERROR_MESSAGE,
            _failure_message_for,
        )

        inner = RuntimeError("Discord bot action 'create_thread' failed: 403 DiscordAPIError[50001]: Missing Access")
        outer = RuntimeError("activity task failed")
        outer.__cause__ = inner
        assert _failure_message_for(outer) == CHANNEL_ACCESS_ERROR_MESSAGE
        assert _failure_message_for(RuntimeError("sandbox died")) == INTERNAL_ERROR_MESSAGE


class TestThreadWatchLifecycle:
    def test_prepare_watches_created_thread(self):
        from posthog.temporal.ai.posthog_code_discord_mention import prepare_discord_thread_activity

        client = MagicMock()
        client.create_thread.return_value = {"thread_id": "t9"}
        client.post_message.return_value = {"message_id": "a1"}
        with patch(
            "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
            return_value=(MagicMock(), client),
        ):
            result = prepare_discord_thread_activity(_inputs())
        client.watch_thread.assert_called_once_with(guild_id="g1", thread_id="t9")
        assert result == {"anchor_message_id": "a1", "thread_id": "t9"}

    def test_prepare_skips_watch_when_thread_creation_fell_back(self):
        from posthog.temporal.ai.posthog_code_discord_mention import prepare_discord_thread_activity

        client = MagicMock()
        client.create_thread.return_value = {}  # bot couldn't create; falls back to channel
        client.post_message.return_value = {"message_id": "a1"}
        with patch(
            "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
            return_value=(MagicMock(), client),
        ):
            prepare_discord_thread_activity(_inputs())
        client.watch_thread.assert_not_called()

    def test_prepare_runs_in_current_thread_without_creating_one(self):
        # `/ph code` invoked inside a thread: threads can't nest, so run the task in it.
        from posthog.temporal.ai.posthog_code_discord_mention import prepare_discord_thread_activity

        inputs = _inputs()
        inputs.interaction["channel_is_thread"] = True
        client = MagicMock()
        client.post_message.return_value = {"message_id": "a1"}
        with patch(
            "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
            return_value=(MagicMock(), client),
        ):
            result = prepare_discord_thread_activity(inputs)
        client.create_thread.assert_not_called()
        client.watch_thread.assert_called_once_with(guild_id="g1", thread_id="c1")
        assert result == {"anchor_message_id": "a1", "thread_id": "c1"}

    def test_terminal_update_unwatches_thread(self, settings):
        settings.DISCORD_BOT_ACTIONS_URL = "http://bot.local/actions"
        settings.DISCORD_BRIDGE_SHARED_SECRET = "s"
        from products.tasks.backend.temporal.process_task.activities.post_discord_update import _unwatch_thread_for_run

        mapping = MagicMock(guild_id="g1", thread_id="t9")
        chain = MagicMock()
        chain.filter.return_value.first.return_value = mapping
        client = MagicMock()
        with (
            patch(
                "products.discord_app.backend.models.DiscordThreadTaskMapping.objects.unscoped",
                return_value=chain,
            ),
            patch("posthog.models.integration.DiscordBotClient", return_value=client) as client_cls,
        ):
            _unwatch_thread_for_run(MagicMock(id="r1"))
        client_cls.assert_called_once()
        client.unwatch_thread.assert_called_once_with(guild_id="g1", thread_id="t9")
