from django.db import migrations
from django.db.models import Count


def dedupe_internal_sandbox_environments(apps, schema_editor):
    """Collapse duplicate internal sandbox environments down to the oldest per (team, name).

    A get-or-create race could insert two internal rows for the same (team, name),
    after which every subsequent lookup raised MultipleObjectsReturned. Keep the
    oldest row, repoint any TaskRun.state references off the surplus rows, and delete
    them. Idempotent: a second run finds no duplicate groups and is a no-op.
    """
    SandboxEnvironment = apps.get_model("tasks", "SandboxEnvironment")
    TaskRun = apps.get_model("tasks", "TaskRun")

    # Duplicate groups are few (one internal env per team/name), so materialize the
    # group list up front rather than holding a cursor open while we delete rows.
    duplicate_groups = list(
        SandboxEnvironment.objects.filter(internal=True)
        .values("team_id", "name")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
    )

    for group in duplicate_groups:
        rows = list(
            SandboxEnvironment.objects.filter(team_id=group["team_id"], name=group["name"], internal=True).order_by(
                "created_at", "id"
            )
        )
        if len(rows) < 2:
            continue

        canonical = rows[0]
        canonical_id = str(canonical.id)
        surplus_ids = [str(env.id) for env in rows[1:]]

        runs = list(TaskRun.objects.filter(state__sandbox_environment_id__in=surplus_ids))
        for run in runs:
            run.state["sandbox_environment_id"] = canonical_id
        for start in range(0, len(runs), 500):
            TaskRun.objects.bulk_update(runs[start : start + 500], ["state"])

        SandboxEnvironment.objects.filter(id__in=[env.id for env in rows[1:]]).delete()


class Migration(migrations.Migration):
    # Non-atomic so each duplicate group commits independently rather than holding
    # locks across the whole table for the duration of the migration.
    atomic = False

    dependencies = [
        ("tasks", "0037_codeworkflowconfig_codeprsnapshot_codeworkstream"),
    ]

    operations = [
        migrations.RunPython(
            dedupe_internal_sandbox_environments,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
