import json

import pytest
from unittest.mock import MagicMock, patch

from django.test import RequestFactory

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
