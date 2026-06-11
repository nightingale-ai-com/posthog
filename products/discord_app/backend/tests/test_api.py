import json

import pytest
from unittest.mock import MagicMock, patch

from django.test import RequestFactory

from posthog.models.integration import Integration

from products.discord_app.backend import api
from products.discord_app.backend.services.integration_resolver import ResolutionResult

SECRET = "bridge-secret"


def _ingest_request(body: dict):
    return RequestFactory().post(
        "/api/discord/interactions/ingest",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {SECRET}",
    )


def _resolution(integration):
    return ResolutionResult(
        integration=integration, source="sole_candidate", candidates=[integration] if integration else []
    )


class TestIngestDispatch:
    # override_settings cannot decorate plain (non-SimpleTestCase) classes
    @pytest.fixture(autouse=True)
    def _bridge_secret(self, settings):
        settings.DISCORD_BRIDGE_SHARED_SECRET = SECRET

    @pytest.fixture(autouse=True)
    def _no_thread_mapping(self):
        # commands in these tests run outside task threads; stub the followup lookup
        chain = MagicMock()
        chain.filter.return_value.select_related.return_value.first.return_value = None
        with patch.object(api.DiscordThreadTaskMapping.objects, "unscoped", return_value=chain):
            yield

    def test_unauthorized_without_bearer(self):
        request = RequestFactory().post("/api/discord/interactions/ingest", data="{}", content_type="application/json")
        assert api.discord_interactions_ingest(request).status_code == 401

    def test_unknown_kind_400(self):
        resp = api.discord_interactions_ingest(_ingest_request({"kind": "nope"}))
        assert resp.status_code == 400

    def test_unconnected_guild_returns_ephemeral(self):
        with patch.object(api, "load_integrations", return_value=_resolution(None)):
            resp = api.discord_interactions_ingest(
                _ingest_request({"kind": "command", "command": "posthog", "guild_id": "g", "user": {"id": "u"}})
            )
        assert json.loads(resp.content)["action"] == "ephemeral"

    def test_unlinked_user_returns_link_prompt(self):
        integration = MagicMock(id=7)
        with (
            patch.object(api, "load_integrations", return_value=_resolution(integration)),
            patch.object(api, "_linked_user", return_value=None),
        ):
            resp = api.discord_interactions_ingest(
                _ingest_request({"kind": "command", "command": "posthog", "guild_id": "g", "user": {"id": "u"}})
            )
        body = json.loads(resp.content)
        assert body["action"] == "ephemeral"
        assert "link your posthog account" in body["content"].lower()

    def test_linked_user_starts_workflow_and_accepts(self):
        integration = MagicMock(id=7)
        link = MagicMock(user_id=42)
        with (
            patch.object(api, "load_integrations", return_value=_resolution(integration)),
            patch.object(api, "_linked_user", return_value=link),
            patch.object(api, "_start_workflow") as start,
        ):
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "command",
                        "command": "posthog",
                        "guild_id": "g",
                        "user": {"id": "u"},
                        "channel_id": "c",
                        "options": {"prompt": "fix readme"},
                        "interaction_id": "i1",
                        "interaction_token": "tok",
                    }
                )
            )
        assert json.loads(resp.content)["status"] == "accepted"
        start.assert_called_once()

    def test_project_set_works_when_no_default_resolved(self):
        # `/posthog-project set` is the escape hatch from ambiguity, so it must not be
        # gated on a resolved integration itself.
        target = MagicMock(id=9, team_id=123)
        ambiguous = ResolutionResult(integration=None, source="needs_picker", candidates=[MagicMock(), MagicMock()])
        with (
            patch.object(api, "load_integrations", return_value=ambiguous),
            patch.object(api, "_integration_for_project_id", return_value=target),
            patch.object(api.commands_dispatch, "handle_project_set", return_value="default set") as set_handler,
        ):
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "command",
                        "command": "posthog-project",
                        "subcommand": "set",
                        "guild_id": "g",
                        "user": {"id": "u"},
                        "options": {"project_id": "123"},
                    }
                )
            )
        body = json.loads(resp.content)
        assert body["action"] == "ephemeral"
        assert body["content"] == "default set"
        set_handler.assert_called_once()

    def test_project_show_lists_candidates_when_ambiguous(self):
        candidate = MagicMock()
        candidate.team_id = 1
        candidate.team.name = "Team One"
        candidate.team.organization.name = "Org"
        ambiguous = ResolutionResult(integration=None, source="needs_picker", candidates=[candidate, MagicMock()])
        with patch.object(api, "load_integrations", return_value=ambiguous):
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "command",
                        "command": "posthog-project",
                        "subcommand": "show",
                        "guild_id": "g",
                        "user": {"id": "u"},
                    }
                )
            )
        body = json.loads(resp.content)
        assert body["action"] == "ephemeral"
        assert "No default project set" in body["content"]
        assert "Team One" in body["content"]

    @pytest.mark.parametrize("command", ["connect", "posthog-connect"])
    def test_connect_command_returns_signed_url_when_unconnected(self, command):
        # no load_integrations patch: connect must not need (or wait on) resolution
        resp = api.discord_interactions_ingest(
            _ingest_request(
                {
                    "kind": "command",
                    "command": command,
                    "guild_id": "g42",
                    "guild_name": "My Server",
                    "user": {"id": "u", "username": "u1", "global_name": None},
                    "options": {"project_id": "7"},
                    "interaction_id": "i1",
                    "interaction_token": "tok",
                }
            )
        )
        body = json.loads(resp.content)
        assert body["action"] == "ephemeral"
        assert body["content"].startswith("Connect this server: ")
        assert "/api/discord/connect/start?state=" in body["content"]

    def test_repo_select_component_signals_workflow(self):
        with patch.object(api, "_signal_workflow") as signal:
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "component",
                        "custom_id": "posthog_code_repo_select:wf-123",
                        "values": ["acme/widgets"],
                    }
                )
            )
        assert json.loads(resp.content)["status"] == "accepted"
        signal.assert_called_once()
        assert signal.call_args.args[0] == "wf-123"
        assert signal.call_args.args[2] == "acme/widgets"


@pytest.mark.django_db
class TestServerConnect:
    def _setup(self, level=None):
        from posthog.models import Organization, Team, User
        from posthog.models.organization import OrganizationMembership

        org = Organization.objects.create(name="Acme")
        team = Team.objects.create(organization=org, name="Web")
        user = User.objects.create_user(email="admin@acme.com", password=None, first_name="A")
        OrganizationMembership.objects.create(
            organization=org, user=user, level=level or OrganizationMembership.Level.ADMIN
        )
        return org, team, user

    def _state(self, user_id=None):
        from django.core import signing

        payload = {"guild_id": "g42", "guild_name": "My Server", "discord_user_id": "du1"}
        if user_id is not None:
            payload["user_id"] = user_id
        return signing.dumps(payload, salt=api.SERVER_CONNECT_SALT)

    def test_start_rejects_bad_state(self):
        _, _, user = self._setup()
        request = RequestFactory().get("/api/discord/connect/start", {"state": "garbage"})
        request.user = user
        assert api.discord_connect_start(request).status_code == 400

    def test_start_renders_picker_for_org_admin(self):
        _, team, user = self._setup()
        request = RequestFactory().get("/api/discord/connect/start", {"state": self._state()})
        request.user = user
        resp = api.discord_connect_start(request)
        assert resp.status_code == 200
        assert "My Server" in resp.content.decode()
        assert f'value="{team.id}"' in resp.content.decode()

    def test_start_forbidden_for_non_admin(self):
        from posthog.models.organization import OrganizationMembership

        _, _, user = self._setup(level=OrganizationMembership.Level.MEMBER)
        request = RequestFactory().get("/api/discord/connect/start", {"state": self._state()})
        request.user = user
        assert api.discord_connect_start(request).status_code == 403

    def test_confirm_creates_integration(self):
        _, team, user = self._setup()
        request = RequestFactory().post(
            "/api/discord/connect/confirm", {"state": self._state(user_id=user.id), "team_id": str(team.id)}
        )
        request.user = user
        resp = api.discord_connect_confirm(request)
        assert resp.status_code == 200
        integration = Integration.objects.get(kind="discord", integration_id="g42")
        assert integration.team_id == team.id
        assert integration.config.get("guild_name") == "My Server"

    def test_confirm_rejects_session_mismatch(self):
        _, team, user = self._setup()
        request = RequestFactory().post(
            "/api/discord/connect/confirm", {"state": self._state(user_id=user.id + 999), "team_id": str(team.id)}
        )
        request.user = user
        assert api.discord_connect_confirm(request).status_code == 403
        assert not Integration.objects.filter(kind="discord").exists()

    def test_confirm_pushes_capture_key_to_bot(self):
        _, team, user = self._setup()
        request = RequestFactory().post(
            "/api/discord/connect/confirm", {"state": self._state(user_id=user.id), "team_id": str(team.id)}
        )
        request.user = user
        client = MagicMock()
        client.connect_guild.return_value = {"ok": True}
        with patch.object(api, "DiscordBotClient", return_value=client):
            resp = api.discord_connect_confirm(request)
        assert resp.status_code == 200
        assert "provisioning analytics" not in resp.content.decode()
        client.connect_guild.assert_called_once_with(guild_id="g42", region="us", project_api_key=team.api_token)

    def test_confirm_surfaces_provisioning_failure(self):
        _, team, user = self._setup()
        request = RequestFactory().post(
            "/api/discord/connect/confirm", {"state": self._state(user_id=user.id), "team_id": str(team.id)}
        )
        request.user = user
        client = MagicMock()
        client.connect_guild.return_value = {"ok": False, "error": "nope"}
        with patch.object(api, "DiscordBotClient", return_value=client):
            resp = api.discord_connect_confirm(request)
        # the binding is persisted even when bot provisioning fails
        assert Integration.objects.filter(kind="discord", integration_id="g42", team=team).exists()
        assert "provisioning analytics on the bot failed" in resp.content.decode()

    def test_reconnect_updates_binding_and_repushes_key(self):
        org, team, user = self._setup()
        from posthog.models import Team

        team2 = Team.objects.create(organization=org, name="Mobile")
        client = MagicMock()
        client.connect_guild.return_value = {"ok": True}
        for target in (team, team2):
            request = RequestFactory().post(
                "/api/discord/connect/confirm", {"state": self._state(user_id=user.id), "team_id": str(target.id)}
            )
            request.user = user
            with patch.object(api, "DiscordBotClient", return_value=client):
                assert api.discord_connect_confirm(request).status_code == 200
        # last connect wins on the bot side...
        assert client.connect_guild.call_args.kwargs["project_api_key"] == team2.api_token
        # ...and for command routing (the bot ships no project-picker command)
        from products.discord_app.backend.models import DiscordSettings

        default = DiscordSettings.objects.get(guild_id="g42", discord_user_id=None)
        assert default.default_integration.team_id == team2.id

    def test_delete_last_binding_pushes_empty_key(self, settings):
        settings.DISCORD_BOT_ACTIONS_URL = "http://bot.local/actions"
        settings.DISCORD_BRIDGE_SHARED_SECRET = "s"
        _, team, user = self._setup()
        integration = Integration.objects.create(team=team, kind="discord", integration_id="g42", created_by=user)
        client = MagicMock()
        with patch("products.discord_app.backend.signals.DiscordBotClient", return_value=client):
            integration.delete()
        client.connect_guild.assert_called_once_with(guild_id="g42", region="us", project_api_key="")


class TestThreadFollowups:
    @pytest.fixture(autouse=True)
    def _bridge_secret(self, settings):
        settings.DISCORD_BRIDGE_SHARED_SECRET = SECRET

    def _mapping(self, owner="du1"):
        mapping = MagicMock()
        mapping.guild_id = "g1"
        mapping.thread_id = "t1"
        mapping.integration_id = 7
        mapping.discord_user_id = owner
        return mapping

    def _patch_mapping(self, mapping):
        chain = MagicMock()
        chain.filter.return_value.select_related.return_value.first.return_value = mapping
        return patch.object(api.DiscordThreadTaskMapping.objects, "unscoped", return_value=chain)

    def test_code_in_task_thread_forwards_as_followup(self):
        mapping = self._mapping()
        with (
            patch.object(api, "load_integrations", return_value=_resolution(MagicMock(id=7))),
            self._patch_mapping(mapping),
            patch.object(api, "_linked_user", return_value=MagicMock(user_id=42)),
            patch.object(api, "_start_workflow") as start,
        ):
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "command",
                        "command": "code",
                        "guild_id": "g1",
                        "channel_id": "t1",
                        "user": {"id": "du1"},
                        "options": {"prompt": "open a draft PR"},
                        "interaction_id": "i9",
                    }
                )
            )
        body = json.loads(resp.content)
        assert "Sent to the running agent" in body["content"]
        start.assert_called_once()
        inputs = start.call_args.args[1]
        assert inputs.text == "open a draft PR"
        assert inputs.thread_id == "t1"
        assert start.call_args.args[2] == "posthog-code-discord-followup-g1:i9"

    def test_plain_thread_message_forwards_for_linked_sender(self):
        mapping = self._mapping(owner="du1")
        with (
            self._patch_mapping(mapping),
            patch.object(api, "_linked_user", return_value=MagicMock(user_id=43)),
            patch.object(api, "_start_workflow") as start,
        ):
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "message",
                        "guild_id": "g1",
                        "channel_id": "t1",
                        "user": {"id": "du2", "username": "sam", "global_name": "Sam"},
                        "content": "use the v2 endpoint",
                        "message_id": "m5",
                    }
                )
            )
        assert json.loads(resp.content)["status"] == "accepted"
        inputs = start.call_args.args[1]
        # different sender than the thread owner gets name-prefixed
        assert inputs.text == "Sam: use the v2 endpoint"

    def test_plain_thread_message_ignored_for_unlinked_or_bot(self):
        mapping = self._mapping()
        with (
            self._patch_mapping(mapping),
            patch.object(api, "_linked_user", return_value=None),
            patch.object(api, "_start_workflow") as start,
        ):
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {"kind": "message", "guild_id": "g1", "channel_id": "t1", "user": {"id": "du9"}, "content": "hi"}
                )
            )
            assert json.loads(resp.content)["status"] == "accepted"
            resp = api.discord_interactions_ingest(
                _ingest_request(
                    {
                        "kind": "message",
                        "guild_id": "g1",
                        "channel_id": "t1",
                        "user": {"id": "botid", "bot": True},
                        "content": "hi",
                    }
                )
            )
            assert json.loads(resp.content)["status"] == "accepted"
        start.assert_not_called()
