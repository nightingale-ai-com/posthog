import re
from dataclasses import dataclass
from typing import Any

import structlog

from posthog.models.integration import DiscordBotClient, DiscordIntegration, Integration

logger = structlog.get_logger(__name__)

UPSTREAM_PROVIDER_FAILURE_MESSAGE = (
    "The upstream AI provider failed to process the request. Please retry the task in a few minutes."
)
UPSTREAM_PROVIDER_ERROR_STATUS_PATTERN = re.compile(r"\bapi error:\s*(?:429|5\d\d)\b", re.IGNORECASE)

# Discord does not expand :shortcodes: in API-sent content (that's a client-side input
# feature), so both reactions and message text below use literal unicode emoji.
# Map the names the relay uses to unicode the bot can react with verbatim.
_REACTION_EMOJI = {
    "eyes": "\U0001f440",  # 👀
    "hedgehog": "\U0001f994",  # 🦔
    "x": "❌",  # ❌
    "rocket": "\U0001f680",  # 🚀
}


def _format_task_error(error: str) -> str:
    error = error.strip()
    if not error:
        return "Unknown error"
    if UPSTREAM_PROVIDER_ERROR_STATUS_PATTERN.search(error):
        return UPSTREAM_PROVIDER_FAILURE_MESSAGE
    return error


def _link_button_row(buttons: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Build a Discord action-row with link-style buttons. `buttons` is (label, url) pairs."""
    if not buttons:
        return []
    return [
        {
            "type": 1,  # action row
            "components": [{"type": 2, "style": 5, "label": label, "url": url} for label, url in buttons],
        }
    ]


@dataclass
class DiscordThreadContext:
    """Context for posting messages back to a Discord thread during task execution.

    Slash commands create no user message, so ``anchor_message_id`` (the deferred
    ``@original``) is the message the bridge reacts to and edits for progress.
    ``interaction_token`` lets the bot edit ``@original`` for ~15 min; after that the bridge
    edits by ``anchor_message_id`` with the bot token.
    """

    integration_id: int
    channel_id: str
    thread_id: str
    anchor_message_id: str | None = None
    interaction_token: str | None = None
    discord_user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "integration_id": self.integration_id,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
        }
        if self.anchor_message_id is not None:
            d["anchor_message_id"] = self.anchor_message_id
        if self.interaction_token is not None:
            d["interaction_token"] = self.interaction_token
        if self.discord_user_id is not None:
            d["discord_user_id"] = self.discord_user_id
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscordThreadContext":
        return cls(
            integration_id=data["integration_id"],
            channel_id=data["channel_id"],
            thread_id=data["thread_id"],
            anchor_message_id=data.get("anchor_message_id"),
            interaction_token=data.get("interaction_token"),
            discord_user_id=data.get("discord_user_id"),
        )


class DiscordThreadHandler:
    """Posts updates to a Discord thread during task execution, via the companion bot's
    actions API. Mirrors ``SlackThreadHandler``'s method surface so the task relay can call
    either transport interchangeably.
    """

    def __init__(self, context: DiscordThreadContext) -> None:
        self.context = context
        self._integration: Integration | None = None
        self._client: DiscordBotClient | None = None

    def _get_integration(self) -> Integration:
        if self._integration is None:
            self._integration = Integration.objects.get(id=self.context.integration_id)
        return self._integration

    def _get_client(self) -> DiscordBotClient:
        if self._client is None:
            self._client = DiscordIntegration(self._get_integration()).client
        return self._client

    @property
    def _post_target(self) -> str:
        return self.context.thread_id or self.context.channel_id

    def update_reaction(self, emoji: str) -> None:
        """Swap the reaction on the anchor message (slash commands have no user message)."""
        anchor = self.context.anchor_message_id
        if not anchor:
            return
        target_emoji = _REACTION_EMOJI.get(emoji, emoji)
        try:
            client = self._get_client()
            try:
                client.remove_reaction(self.context.channel_id, anchor, _REACTION_EMOJI["eyes"])
            except Exception:
                pass
            client.add_reaction(self.context.channel_id, anchor, target_emoji)
        except Exception as e:
            logger.warning("discord_update_reaction_failed", error=str(e))

    def post_or_update_progress(self, stage: str, task_url: str | None = None) -> None:
        """Edit the anchor message to reflect the current stage (a single evolving status)."""
        content = f"**Working on task…** ⏳\nStage: {stage}"
        components = _link_button_row([("View agent logs", task_url)] if task_url else [])
        try:
            client = self._get_client()
            if self.context.anchor_message_id:
                client.edit_message(
                    target_id=self.context.channel_id,
                    message_id=self.context.anchor_message_id,
                    content=content,
                    components=components,
                    interaction_token=self.context.interaction_token,
                )
            else:
                client.post_message(target_id=self._post_target, content=content, components=components)
        except Exception as e:
            logger.exception("discord_progress_update_failed", error=str(e))

    def post_pr_opened_sandbox_cleaned(self, pr_url: str, task_url: str | None) -> None:
        buttons = [("View PR", pr_url)]
        if task_url:
            buttons.append(("Open in PostHog", task_url))
        self._post_thread("**Pull request opened** 🚀", _link_button_row(buttons))

    def post_pr_opened(self, pr_url: str, task_url: str | None) -> None:
        mention = f"<@{self.context.discord_user_id}> " if self.context.discord_user_id else ""
        buttons = [("View PR", pr_url)]
        if task_url:
            buttons.append(("Open in PostHog", task_url))
        self._post_thread(f"{mention}Pull request opened.", _link_button_row(buttons))

    def post_thread_message(self, text: str) -> None:
        self._post_thread(text, [])

    def post_completion(self, pr_url: str | None, task_url: str | None) -> None:
        header = "**Pull request created** 🚀" if pr_url else "**Task completed** 🦔"
        buttons: list[tuple[str, str]] = []
        if pr_url:
            buttons.append(("View PR", pr_url))
        if task_url:
            buttons.append(("Open in PostHog", task_url))
        self._post_thread(header, _link_button_row(buttons))

    def post_error(self, error: str, task_url: str | None) -> None:
        error = _format_task_error(error)
        truncated = error[:200]
        content = f"**Task failed** ❌\n{truncated}"
        buttons = [("See details in PostHog", task_url)] if task_url else []
        self._post_thread(content, _link_button_row(buttons))

    def post_cancelled(self, task_url: str | None) -> None:
        buttons = [("Open in PostHog", task_url)] if task_url else []
        self._post_thread("**Sandbox stopped** 🦔", _link_button_row(buttons))

    def finalize_placeholder(self, content: str) -> None:
        """Replace the deferred interaction placeholder ("posthog is thinking…") with a
        final status. Token-only edits target Discord's ``@original``; the token expires
        ~15 minutes after the slash command, so this is best-effort by design.
        """
        if not self.context.interaction_token:
            return
        try:
            self._get_client().edit_message(content=content, interaction_token=self.context.interaction_token)
        except Exception as e:
            logger.warning("discord_finalize_placeholder_failed", error=str(e))

    def delete_progress(self) -> None:
        # Progress lives on the anchor message (edited in place), so there is no separate
        # progress message to delete. No-op, kept for parity with SlackThreadHandler.
        return

    def _post_thread(self, content: str, components: list[dict[str, Any]]) -> None:
        try:
            self._get_client().post_message(
                target_id=self._post_target,
                content=content,
                components=components or None,
            )
        except Exception as e:
            logger.exception("discord_thread_post_failed", error=str(e))
