from django.db import migrations
from django.db.models import Count

# Tuned small so each batch holds short-lived locks and bounded memory.
GROUP_BATCH = 100
REF_BATCH = 500
DELETE_BATCH = 500


def dedupe_sandbox_environments(apps, schema_editor):
    """Collapse duplicate sandbox environments down to the most recent per (team, name).

    Names become unique per team in the following migration, so any pre-existing
    duplicates (from the get-or-create race, or from the API never validating names)
    must be resolved first. Keep the most recently created row, repoint any
    TaskRun.state references off the surplus rows, and delete them.

    Batched and non-atomic: duplicate groups are processed in chunks and each
    delete/repoint commits independently, so the migration never holds a long lock
    or pulls the whole table into memory. Idempotent: once every (team, name) has a
    single row there are no groups left and the loop exits.
    """
    SandboxEnvironment = apps.get_model("tasks", "SandboxEnvironment")
    TaskRun = apps.get_model("tasks", "TaskRun")

    while True:
        groups = list(
            SandboxEnvironment.objects.values("team_id", "name")
            .annotate(row_count=Count("id"))
            .filter(row_count__gt=1)
            .order_by()[:GROUP_BATCH]
        )
        if not groups:
            break

        for group in groups:
            row_ids = list(
                SandboxEnvironment.objects.filter(team_id=group["team_id"], name=group["name"])
                .order_by("-created_at", "-id")
                .values_list("id", flat=True)
            )
            keeper_id = str(row_ids[0])
            surplus_ids = row_ids[1:]
            surplus_str = [str(env_id) for env_id in surplus_ids]

            # Repoint references (TaskRun.state JSON; there are no DB FKs to this
            # model) a batch of runs at a time. Updated runs no longer match the
            # filter, so the loop drains them.
            while True:
                run_ids = list(
                    TaskRun.objects.filter(state__sandbox_environment_id__in=surplus_str).values_list("id", flat=True)[
                        :REF_BATCH
                    ]
                )
                if not run_ids:
                    break
                runs = list(TaskRun.objects.filter(id__in=run_ids))
                for run in runs:
                    run.state["sandbox_environment_id"] = keeper_id
                TaskRun.objects.bulk_update(runs, ["state"])

            for start in range(0, len(surplus_ids), DELETE_BATCH):
                SandboxEnvironment.objects.filter(id__in=surplus_ids[start : start + DELETE_BATCH]).delete()


class Migration(migrations.Migration):
    # Non-atomic so each batch commits independently rather than holding locks
    # across the whole table for the duration of the migration.
    atomic = False

    dependencies = [
        ("tasks", "0037_codeworkflowconfig_codeprsnapshot_codeworkstream"),
    ]

    operations = [
        migrations.RunPython(
            dedupe_sandbox_environments,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
