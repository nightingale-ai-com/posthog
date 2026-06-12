from posthog.test.base import BaseTest

from django.db import connection
from django.utils import timezone

from products.signals.backend.temporal.agentic import SIGNALS_REPO_DISCOVERY_ENV_NAME, get_or_create_signals_sandbox_env
from products.tasks.backend.models import SandboxEnvironment, Task, TaskRun

_UNIQUE_INDEX = "unique_internal_sandbox_env_per_team_name"


class TestGetOrCreateSignalsSandboxEnv(BaseTest):
    def _drop_internal_unique_index(self) -> None:
        # Simulate the pre-constraint production state so we can insert duplicate
        # internal rows. The DROP is rolled back with the test transaction.
        with connection.cursor() as cursor:
            cursor.execute(f"DROP INDEX IF EXISTS {_UNIQUE_INDEX}")

    def test_creates_internal_environment(self):
        env_id = get_or_create_signals_sandbox_env(
            self.team.id,
            SIGNALS_REPO_DISCOVERY_ENV_NAME,
            SandboxEnvironment.NetworkAccessLevel.TRUSTED,
        )

        env = SandboxEnvironment.objects.get(id=env_id)
        self.assertTrue(env.internal)
        self.assertFalse(env.private)
        self.assertEqual(env.network_access_level, SandboxEnvironment.NetworkAccessLevel.TRUSTED)

    def test_repeated_calls_produce_a_single_row(self):
        first = get_or_create_signals_sandbox_env(
            self.team.id,
            SIGNALS_REPO_DISCOVERY_ENV_NAME,
            SandboxEnvironment.NetworkAccessLevel.TRUSTED,
        )
        second = get_or_create_signals_sandbox_env(
            self.team.id,
            SIGNALS_REPO_DISCOVERY_ENV_NAME,
            SandboxEnvironment.NetworkAccessLevel.FULL,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            SandboxEnvironment.objects.filter(
                team_id=self.team.id, name=SIGNALS_REPO_DISCOVERY_ENV_NAME, internal=True
            ).count(),
            1,
        )
        # The policy is reasserted on every call.
        env = SandboxEnvironment.objects.get(id=second)
        self.assertEqual(env.network_access_level, SandboxEnvironment.NetworkAccessLevel.FULL)

    def test_does_not_clobber_non_internal_environment_with_same_name(self):
        user_env = SandboxEnvironment.objects.create(
            team=self.team,
            created_by=self.user,
            name=SIGNALS_REPO_DISCOVERY_ENV_NAME,
            internal=False,
            private=True,
        )

        env_id = get_or_create_signals_sandbox_env(
            self.team.id,
            SIGNALS_REPO_DISCOVERY_ENV_NAME,
            SandboxEnvironment.NetworkAccessLevel.TRUSTED,
        )

        self.assertNotEqual(str(user_env.id), env_id)
        user_env.refresh_from_db()
        self.assertFalse(user_env.internal)
        self.assertTrue(user_env.private)

    def test_recovers_from_pre_existing_duplicates(self):
        self._drop_internal_unique_index()

        older = SandboxEnvironment.objects.create(
            team=self.team,
            name=SIGNALS_REPO_DISCOVERY_ENV_NAME,
            internal=True,
            private=False,
            network_access_level=SandboxEnvironment.NetworkAccessLevel.FULL,
        )
        newer = SandboxEnvironment.objects.create(
            team=self.team,
            name=SIGNALS_REPO_DISCOVERY_ENV_NAME,
            internal=True,
            private=False,
            network_access_level=SandboxEnvironment.NetworkAccessLevel.FULL,
        )
        # auto_now_add ignores assigned values, so pin created_at explicitly.
        SandboxEnvironment.objects.filter(id=older.id).update(created_at=timezone.now() - timezone.timedelta(hours=1))

        task = Task.objects.create(
            team=self.team,
            title="Signal report",
            description="",
            origin_product=Task.OriginProduct.SIGNAL_REPORT,
        )
        run = TaskRun.objects.create(
            task=task,
            team=self.team,
            state={"sandbox_environment_id": str(newer.id)},
        )

        env_id = get_or_create_signals_sandbox_env(
            self.team.id,
            SIGNALS_REPO_DISCOVERY_ENV_NAME,
            SandboxEnvironment.NetworkAccessLevel.TRUSTED,
        )

        # The oldest row is canonical; the surplus duplicate is removed.
        self.assertEqual(env_id, str(older.id))
        remaining = SandboxEnvironment.objects.filter(
            team_id=self.team.id, name=SIGNALS_REPO_DISCOVERY_ENV_NAME, internal=True
        )
        self.assertEqual(list(remaining.values_list("id", flat=True)), [older.id])
        # Policy is reasserted on the canonical row.
        self.assertEqual(remaining.get().network_access_level, SandboxEnvironment.NetworkAccessLevel.TRUSTED)
        # References on the surplus row are repointed to the canonical row.
        run.refresh_from_db()
        self.assertEqual(run.state["sandbox_environment_id"], str(older.id))
