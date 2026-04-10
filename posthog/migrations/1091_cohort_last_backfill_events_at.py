from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "1090_batchexportrun_records_failed"),
    ]

    operations = [
        migrations.AddField(
            model_name="cohort",
            name="last_backfill_events_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
