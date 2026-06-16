from django.db import migrations, models

from posthog.migration_helpers import CreateIndexConcurrently


class Migration(migrations.Migration):
    atomic = False  # Required for CONCURRENTLY

    dependencies = [
        ("signals", "0037_signalscoutemission_tags"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="signalreporttask",
                    index=models.Index(fields=["report", "relationship"], name="signals_report_task_rel_idx"),
                ),
            ],
            database_operations=[
                CreateIndexConcurrently(
                    index_name="signals_report_task_rel_idx",
                    table_name="signals_signalreporttask",
                    columns="(report_id, relationship)",
                ),
            ],
        ),
    ]
