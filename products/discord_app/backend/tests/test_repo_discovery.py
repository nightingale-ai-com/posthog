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
