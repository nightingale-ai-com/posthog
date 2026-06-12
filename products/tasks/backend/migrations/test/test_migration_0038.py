from typing import Any

import pytest
from posthog.test.base import NonAtomicTestMigrations

# skipped in CI because migration tests are slow; passes locally.
# To run it, comment this out: `hogli test products/tasks/backend/migrations/test/test_migration_0038.py`
pytestmark = pytest.mark.skip("historical migration tests slow overall test run")


class DedupeInternalSandboxEnvironmentsMigrationTest(NonAtomicTestMigrations):
    migrate_from = "0037_codeworkflowconfig_codeprsnapshot_codeworkstream"
    migrate_to = "0038_dedupe_internal_sandbox_environments"

    CLASS_DATA_LEVEL_SETUP = False

    @property
    def app(self) -> str:
        return "tasks"

    def setUp(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        migrate_from = [
            ("tasks", self.migrate_from),
            ("posthog", "1166_oauth_impersonated_by"),
        ]
        migrate_to = [("tasks", self.migrate_to)]

        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(migrate_from).apps
        executor.migrate(migrate_from)

        self.setUpBeforeMigration(old_apps)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(migrate_to)

        self.apps = executor.loader.project_state(migrate_to).apps

    def setUpBeforeMigration(self, apps: Any) -> None:
        Organization = apps.get_model("posthog", "Organization")
        Project = apps.get_model("posthog", "Project")
        Team = apps.get_model("posthog", "Team")
        SandboxEnvironment = apps.get_model("tasks", "SandboxEnvironment")
        Task = apps.get_model("tasks", "Task")
        TaskRun = apps.get_model("tasks", "TaskRun")

        org = Organization.objects.create(name="Test Organization")
        project = Project.objects.create(id=987654, organization=org, name="Test Project")
        team = Team.objects.create(organization=org, project=project, name="Test Team")
        self.team_id = team.id

        # At migration state 0037 the partial unique constraint does not exist yet,
        # so we can insert the duplicate internal rows that the race produced.
        self.keeper = SandboxEnvironment.objects.create(team=team, name="SIGNALS_REPO_DISCOVERY", internal=True)
        SandboxEnvironment.objects.filter(id=self.keeper.id).update(created_at="2024-01-01T00:00:00Z")
        self.surplus = SandboxEnvironment.objects.create(team=team, name="SIGNALS_REPO_DISCOVERY", internal=True)
        SandboxEnvironment.objects.filter(id=self.surplus.id).update(created_at="2024-02-01T00:00:00Z")

        # A non-internal env sharing the name and a distinct internal env must be untouched.
        self.user_env = SandboxEnvironment.objects.create(team=team, name="SIGNALS_REPO_DISCOVERY", internal=False)
        self.other_internal = SandboxEnvironment.objects.create(
            team=team, name="SIGNALS_REPORT_RESEARCH", internal=True
        )

        task = Task.objects.create(
            team=team,
            title="Signal report",
            description="",
            origin_product="signal_report",
        )
        self.run = TaskRun.objects.create(
            task=task,
            team=team,
            state={"sandbox_environment_id": str(self.surplus.id)},
        )

    def test_keeps_oldest_internal_row(self):
        SandboxEnvironment = self.apps.get_model("tasks", "SandboxEnvironment")
        survivors = SandboxEnvironment.objects.filter(
            team_id=self.team_id, name="SIGNALS_REPO_DISCOVERY", internal=True
        )
        self.assertEqual(list(survivors.values_list("id", flat=True)), [self.keeper.id])

    def test_repoints_references_to_keeper(self):
        TaskRun = self.apps.get_model("tasks", "TaskRun")
        run = TaskRun.objects.get(id=self.run.id)
        self.assertEqual(run.state["sandbox_environment_id"], str(self.keeper.id))

    def test_leaves_non_internal_and_distinct_internal_rows_untouched(self):
        SandboxEnvironment = self.apps.get_model("tasks", "SandboxEnvironment")
        self.assertTrue(SandboxEnvironment.objects.filter(id=self.user_env.id).exists())
        self.assertTrue(SandboxEnvironment.objects.filter(id=self.other_internal.id).exists())
