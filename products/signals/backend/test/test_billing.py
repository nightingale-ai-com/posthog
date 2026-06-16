import json
from datetime import UTC, datetime

from posthog.test.base import BaseTest

from parameterized import parameterized

from posthog.models import Team

from products.signals.backend.billing import SIGNALS_PRIORITY_CREDITS, get_signals_billing_credits_by_team
from products.signals.backend.models import SignalReport, SignalReportArtefact, SignalReportTask
from products.tasks.backend.models import Task, TaskRun

PERIOD_START = datetime(2026, 6, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 7, 1, tzinfo=UTC)


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=UTC)


class TestSignalsBilling(BaseTest):
    def _report(
        self,
        *,
        team: Team | None = None,
        priority: str | None = "P0",
        actionability: str | None = "immediately_actionable",
        status: str = SignalReport.Status.READY,
    ) -> SignalReport:
        team = team or self.team
        report = SignalReport.objects.create(team=team, status=status, signal_count=1, total_weight=1.0)
        if priority is not None:
            SignalReportArtefact.objects.create(
                team=team,
                report=report,
                type=SignalReportArtefact.ArtefactType.PRIORITY_JUDGMENT,
                content=json.dumps({"explanation": "x", "priority": priority}),
            )
        if actionability is not None:
            SignalReportArtefact.objects.create(
                team=team,
                report=report,
                type=SignalReportArtefact.ArtefactType.ACTIONABILITY_JUDGMENT,
                content=json.dumps({"explanation": "x", "actionability": actionability, "already_addressed": False}),
            )
        return report

    def _pr_run(
        self,
        report: SignalReport,
        *,
        created_at: datetime,
        pr_url: str | None = "https://github.com/x/y/pull/1",
        team: Team | None = None,
        relationship: str = SignalReportTask.Relationship.IMPLEMENTATION,
    ) -> TaskRun:
        team = team or self.team
        task = Task.objects.create(
            team=team, title="impl", description="d", origin_product=Task.OriginProduct.SIGNAL_REPORT
        )
        SignalReportTask.objects.create(team=team, report=report, task=task, relationship=relationship)
        return TaskRun.objects.create(
            team=team, task=task, output=({"pr_url": pr_url} if pr_url is not None else {}), created_at=created_at
        )

    def _credits(self) -> dict[int, int]:
        return dict(get_signals_billing_credits_by_team(PERIOD_START, PERIOD_END))

    def test_credit_map_matches_dollar_pricing(self) -> None:
        # 1 credit = $0.01: P0 $24, P1 $15, P2 $5, P3/P4 $1.
        self.assertEqual(SIGNALS_PRIORITY_CREDITS, {"P0": 2400, "P1": 1500, "P2": 500, "P3": 100, "P4": 100})

    @parameterized.expand([("P0", 2400), ("P1", 1500), ("P2", 500), ("P3", 100), ("P4", 100)])
    def test_actionable_report_with_pr_billed_by_priority(self, priority: str, expected: int) -> None:
        report = self._report(priority=priority)
        self._pr_run(report, created_at=_at(10))
        self.assertEqual(self._credits(), {self.team.id: expected})

    @parameterized.expand([("not_actionable",), ("requires_human_input",)])
    def test_non_actionable_report_not_billed(self, actionability: str) -> None:
        report = self._report(priority="P0", actionability=actionability)
        self._pr_run(report, created_at=_at(10))
        self.assertEqual(self._credits(), {})

    def test_report_without_actionability_artefact_not_billed(self) -> None:
        report = self._report(priority="P0", actionability=None)
        self._pr_run(report, created_at=_at(10))
        self.assertEqual(self._credits(), {})

    def test_report_without_pr_not_billed(self) -> None:
        self._report(priority="P0")
        self.assertEqual(self._credits(), {})

    @parameterized.expand([("null_pr_url", None), ("empty_pr_url", "")])
    def test_run_without_usable_pr_url_not_billed(self, _name: str, pr_url: str | None) -> None:
        report = self._report(priority="P0")
        self._pr_run(report, created_at=_at(10), pr_url=pr_url)
        self.assertEqual(self._credits(), {})

    def test_pr_before_period_not_billed(self) -> None:
        report = self._report(priority="P0")
        self._pr_run(report, created_at=datetime(2026, 5, 28, tzinfo=UTC))
        self.assertEqual(self._credits(), {})

    def test_pr_after_period_not_billed(self) -> None:
        report = self._report(priority="P0")
        self._pr_run(report, created_at=datetime(2026, 7, 2, tzinfo=UTC))
        self.assertEqual(self._credits(), {})

    def test_report_first_billed_in_prior_period_not_rebilled(self) -> None:
        # First PR landed last month; a second PR this month must not re-charge.
        report = self._report(priority="P0")
        self._pr_run(report, created_at=datetime(2026, 5, 28, tzinfo=UTC))
        self._pr_run(report, created_at=_at(20))
        self.assertEqual(self._credits(), {})

    def test_multiple_prs_in_period_billed_once(self) -> None:
        report = self._report(priority="P0")
        self._pr_run(report, created_at=_at(5))
        self._pr_run(report, created_at=_at(22))
        self.assertEqual(self._credits(), {self.team.id: 2400})

    def test_actionable_report_without_priority_skipped(self) -> None:
        report = self._report(priority=None)
        self._pr_run(report, created_at=_at(18))
        self.assertEqual(self._credits(), {})

    def test_pr_on_non_implementation_task_not_billed(self) -> None:
        report = self._report(priority="P1")
        self._pr_run(report, created_at=_at(19), relationship=SignalReportTask.Relationship.RESEARCH)
        self.assertEqual(self._credits(), {})

    @parameterized.expand([(SignalReport.Status.RESOLVED,), (SignalReport.Status.SUPPRESSED,)])
    def test_billed_regardless_of_status_after_landing(self, status: str) -> None:
        report = self._report(priority="P0", status=status)
        self._pr_run(report, created_at=_at(8))
        self.assertEqual(self._credits(), {self.team.id: 2400})

    def test_first_run_with_pr_url_determines_period_not_first_run(self) -> None:
        # An earlier run with no PR URL must not count as the first PR; the in-period PR run does.
        report = self._report(priority="P2")
        self._pr_run(report, created_at=datetime(2026, 5, 3, tzinfo=UTC), pr_url=None)
        self._pr_run(report, created_at=_at(21))
        self.assertEqual(self._credits(), {self.team.id: 500})

    def test_aggregates_across_teams(self) -> None:
        team_b = Team.objects.create(organization=self.organization, name="team-b")
        for priority in ("P0", "P1", "P2"):
            self._pr_run(self._report(priority=priority), created_at=_at(10))
        self._pr_run(self._report(team=team_b, priority="P0"), created_at=_at(11), team=team_b)
        self._pr_run(self._report(team=team_b, priority="P3"), created_at=_at(13), team=team_b)
        self.assertEqual(self._credits(), {self.team.id: 4400, team_b.id: 2500})

    def test_deterministic_across_runs(self) -> None:
        report = self._report(priority="P0")
        self._pr_run(report, created_at=_at(5))
        self._pr_run(report, created_at=_at(22))
        self.assertEqual(self._credits(), self._credits())

    def test_no_billable_reports_returns_empty(self) -> None:
        self.assertEqual(get_signals_billing_credits_by_team(PERIOD_START, PERIOD_END), [])
