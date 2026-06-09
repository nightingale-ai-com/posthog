import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q

import posthog.models.utils


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("posthog", "1217_project_is_pending_deletion"),
        ("tasks", "0038_alter_task_origin_product"),
    ]

    operations = [
        migrations.CreateModel(
            name="DiscordChannel",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=posthog.models.utils.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("guild_id", models.CharField(max_length=64)),
                ("channel_id", models.CharField(max_length=64)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="The PostHog user who clicked Approve. Carries the email and audit trail.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_discord_channels",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DiscordSettings",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=posthog.models.utils.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("guild_id", models.CharField(max_length=64)),
                ("discord_user_id", models.CharField(blank=True, max_length=64, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "default_integration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_settings_as_default",
                        to="posthog.integration",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DiscordUserLink",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=posthog.models.utils.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("discord_user_id", models.CharField(max_length=64)),
                ("discord_username", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "integration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_user_links",
                        to="posthog.integration",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_user_links",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DiscordThreadTaskMapping",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=posthog.models.utils.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("guild_id", models.CharField(max_length=64)),
                ("channel_id", models.CharField(max_length=64)),
                ("thread_id", models.CharField(max_length=64)),
                ("anchor_message_id", models.CharField(blank=True, default="", max_length=64)),
                ("discord_user_id", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "integration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_thread_task_mappings",
                        to="posthog.integration",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_thread_mappings",
                        to="tasks.task",
                    ),
                ),
                (
                    "task_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_thread_mappings",
                        to="tasks.taskrun",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discord_thread_task_mappings",
                        to="posthog.team",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="discordchannel",
            constraint=models.UniqueConstraint(fields=("guild_id", "channel_id"), name="uniq_discord_channel"),
        ),
        migrations.AddConstraint(
            model_name="discordsettings",
            constraint=models.UniqueConstraint(
                condition=Q(("discord_user_id__isnull", False)),
                fields=("guild_id", "discord_user_id"),
                name="uniq_discord_settings_per_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="discordsettings",
            constraint=models.UniqueConstraint(
                condition=Q(("discord_user_id__isnull", True)),
                fields=("guild_id",),
                name="uniq_discord_settings_per_guild",
            ),
        ),
        migrations.AddConstraint(
            model_name="discorduserlink",
            constraint=models.UniqueConstraint(
                fields=("integration", "discord_user_id"), name="uniq_discord_user_link"
            ),
        ),
        migrations.AddConstraint(
            model_name="discordthreadtaskmapping",
            constraint=models.UniqueConstraint(
                fields=("integration", "channel_id", "thread_id"), name="uniq_discord_thread_task_mapping"
            ),
        ),
    ]
