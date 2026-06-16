from django.db import migrations, models

from posthog.migration_helpers import CreateIndexConcurrently


class Migration(migrations.Migration):
    atomic = False  # Required for CONCURRENTLY

    dependencies = [
        ("tasks", "0037_codeworkflowconfig_codeprsnapshot_codeworkstream"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="taskrun",
                    index=models.Index(fields=["created_at"], name="task_run_created_at_idx"),
                ),
            ],
            database_operations=[
                CreateIndexConcurrently(
                    index_name="task_run_created_at_idx",
                    table_name="posthog_task_run",
                    columns="(created_at)",
                ),
            ],
        ),
    ]
