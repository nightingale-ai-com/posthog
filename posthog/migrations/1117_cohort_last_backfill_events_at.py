from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "1116_datadeletionrequest_hogql_predicate"),
    ]

    operations = [
        migrations.AddField(
            model_name="cohort",
            name="last_backfill_events_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
