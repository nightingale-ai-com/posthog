import pytest
from unittest.mock import MagicMock

from django.test import override_settings

from posthog.models.integration import DiscordIntegrationError, verify_discord_bridge_bearer

from products.discord_app.backend.discord_thread import DiscordThreadContext, DiscordThreadHandler
from products.discord_app.backend.repos import repo_autocomplete_choices


def _request_with_auth(header: str | None):
    request = MagicMock()
    request.headers = {"Authorization": header} if header is not None else {}
    return request


class TestBearerVerification:
    @override_settings(DISCORD_BRIDGE_SHARED_SECRET="s3cret")
    def test_accepts_matching_bearer(self):
        verify_discord_bridge_bearer(_request_with_auth("Bearer s3cret"))

    @override_settings(DISCORD_BRIDGE_SHARED_SECRET="s3cret")
    @pytest.mark.parametrize(
        "header",
        [None, "", "Bearer wrong", "s3cret", "Bearer s3cre", "Bearer s3crett"],
    )
    def test_rejects_bad_bearer(self, header):
        with pytest.raises(DiscordIntegrationError):
            verify_discord_bridge_bearer(_request_with_auth(header))

    @override_settings(DISCORD_BRIDGE_SHARED_SECRET="")
    def test_rejects_when_unconfigured(self):
        with pytest.raises(DiscordIntegrationError):
            verify_discord_bridge_bearer(_request_with_auth("Bearer anything"))


class TestRepoAutocomplete:
    def _patch_repos(self, monkeypatch, repos):
        monkeypatch.setattr(
            "products.discord_app.backend.repos.get_full_repo_names",
            lambda integration, *, user_id: repos,
        )

    def test_filters_by_query(self, monkeypatch):
        self._patch_repos(monkeypatch, ["posthog/posthog", "posthog/posthog.com", "acme/widgets"])
        choices = repo_autocomplete_choices(MagicMock(), user_id=1, query="acme")
        assert choices == [{"name": "acme/widgets", "value": "acme/widgets"}]

    def test_empty_query_returns_all(self, monkeypatch):
        self._patch_repos(monkeypatch, ["a/b", "c/d"])
        choices = repo_autocomplete_choices(MagicMock(), user_id=1, query="")
        assert [c["value"] for c in choices] == ["a/b", "c/d"]

    def test_caps_at_25(self, monkeypatch):
        self._patch_repos(monkeypatch, [f"org/repo{i}" for i in range(40)])
        choices = repo_autocomplete_choices(MagicMock(), user_id=1, query="org")
        assert len(choices) == 25


class TestDiscordThreadHandler:
    def _handler(self):
        context = DiscordThreadContext(
            integration_id=1,
            channel_id="thread123",
            thread_id="thread123",
            anchor_message_id="anchor1",
            interaction_token="tok",
            discord_user_id="user1",
        )
        handler = DiscordThreadHandler(context)
        handler._client = MagicMock()
        handler._integration = MagicMock()
        return handler, handler._client

    def test_update_reaction_swaps_eyes_for_target(self):
        handler, client = self._handler()
        handler.update_reaction("hedgehog")
        client.remove_reaction.assert_called_once_with("thread123", "anchor1", "\U0001f440")
        client.add_reaction.assert_called_once_with("thread123", "anchor1", "\U0001f994")

    def test_progress_edits_anchor(self):
        handler, client = self._handler()
        handler.post_or_update_progress("In progress...", task_url=None)
        client.edit_message.assert_called_once()
        kwargs = client.edit_message.call_args.kwargs
        assert kwargs["message_id"] == "anchor1"
        assert kwargs["interaction_token"] == "tok"

    def test_completion_posts_into_thread(self):
        handler, client = self._handler()
        handler.post_completion(pr_url="https://gh/pr/1", task_url=None)
        client.post_message.assert_called_once()
        assert client.post_message.call_args.kwargs["target_id"] == "thread123"

    def test_reaction_noop_without_anchor(self):
        handler, client = self._handler()
        handler.context.anchor_message_id = None
        handler.update_reaction("x")
        client.add_reaction.assert_not_called()
