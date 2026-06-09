from django.db import models
from django.db.models import Q

from posthog.models.scoping.root_mixin import TeamScopedRootMixin
from posthog.models.utils import UUIDModel


class DiscordThreadTaskMapping(TeamScopedRootMixin, UUIDModel):
    """Maps Discord threads to task runs so follow-up messages can be forwarded to the running agent.

    Slash commands have no user message to react to, so ``anchor_message_id`` records the
    message the bridge reacts to and edits for progress (the deferred ``@original``).
    """

    team = models.ForeignKey("posthog.Team", on_delete=models.CASCADE, related_name="discord_thread_task_mappings")
    integration = models.ForeignKey(
        "posthog.Integration",
        on_delete=models.CASCADE,
        related_name="discord_thread_task_mappings",
    )
    guild_id = models.CharField(max_length=64)
    channel_id = models.CharField(max_length=64)
    thread_id = models.CharField(max_length=64)
    anchor_message_id = models.CharField(max_length=64, blank=True, default="")
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="discord_thread_mappings",
    )
    task_run = models.ForeignKey(
        "tasks.TaskRun",
        on_delete=models.CASCADE,
        related_name="discord_thread_mappings",
    )
    discord_user_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "channel_id", "thread_id"],
                name="uniq_discord_thread_task_mapping",
            )
        ]


class DiscordUserLink(UUIDModel):
    """Links a Discord user to a PostHog user.

    Discord bots cannot read member emails, so identity is established by an explicit
    OAuth account-link (``identify`` scope) rather than email matching. Until a row exists
    for ``(integration, discord_user_id)`` the bridge refuses task creation and prompts the
    user to link.
    """

    integration = models.ForeignKey(
        "posthog.Integration",
        on_delete=models.CASCADE,
        related_name="discord_user_links",
    )
    discord_user_id = models.CharField(max_length=64)
    discord_username = models.CharField(max_length=255, blank=True, default="")
    user = models.ForeignKey(
        "posthog.User",
        on_delete=models.CASCADE,
        related_name="discord_user_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "discord_user_id"],
                name="uniq_discord_user_link",
            )
        ]


class DiscordSettings(UUIDModel):
    """Per-(Discord guild, Discord user) routing default — which PostHog integration a
    command from this Discord user should route to.

    Mirrors ``SlackSettings``: a user-specific row (``discord_user_id`` set) wins over the
    guild-wide fallback (``discord_user_id IS NULL``) at resolution time.
    """

    default_integration = models.ForeignKey(
        "posthog.Integration",
        on_delete=models.CASCADE,
        related_name="discord_settings_as_default",
    )
    guild_id = models.CharField(max_length=64)
    discord_user_id = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["guild_id", "discord_user_id"],
                name="uniq_discord_settings_per_user",
                condition=Q(discord_user_id__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["guild_id"],
                name="uniq_discord_settings_per_guild",
                condition=Q(discord_user_id__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        who = self.discord_user_id or "(guild default)"
        return f"{self.guild_id} / {who} → integration {self.default_integration_id}"


class DiscordChannel(UUIDModel):
    """Per-(Discord guild, Discord channel) approval state.

    A row with ``approved_at`` set means a member has acknowledged that PostHog data the
    bot surfaces in this channel may be visible to its members. Mirrors ``SlackChannel`` —
    guild-scoped, not integration-scoped.
    """

    guild_id = models.CharField(max_length=64)
    channel_id = models.CharField(max_length=64)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "posthog.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_discord_channels",
        help_text="The PostHog user who clicked Approve. Carries the email and audit trail.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["guild_id", "channel_id"],
                name="uniq_discord_channel",
            )
        ]

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None
