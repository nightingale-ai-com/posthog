"""Signals billing — credits per actionable report whose implementation produced a PR.

Signals is billed on outcomes, not LLM spend: each report that lands an implementation
PR is charged a flat number of credits based on its priority. The chargeable moment is
deterministic — the `created_at` of the *first* implementation `TaskRun` with a `pr_url`
set — so a report is billed exactly once, in the period that PR first appeared, regardless
of any later status changes. Priority and actionability are read as of that moment (the
newest judgment artefact at or before the first PR), so a later re-judgement can't change a
past period's bill.

Credits use the same unit as ai_credits: 1 credit = $0.01.
"""

import json
import uuid
from collections import defaultdict
from datetime import datetime

from django.db.models import F

import structlog

from products.signals.backend.models import SignalReportArtefact, SignalReportTask
from products.signals.backend.report_generation.research import ActionabilityChoice, Priority
from products.tasks.backend.models import TaskRun

logger = structlog.get_logger(__name__)

_IMPLEMENTATION = SignalReportTask.Relationship.IMPLEMENTATION

SIGNALS_CREDITS_PER_DOLLAR = 100  # 1 credit = $0.01, matching ai_credits

# Flat credits charged per actionable report that shipped a PR, keyed by priority.
SIGNALS_PRIORITY_CREDITS: dict[str, int] = {
    Priority.P0.value: 24 * SIGNALS_CREDITS_PER_DOLLAR,  # 2400
    Priority.P1.value: 15 * SIGNALS_CREDITS_PER_DOLLAR,  # 1500
    Priority.P2.value: 5 * SIGNALS_CREDITS_PER_DOLLAR,  # 500
    Priority.P3.value: 1 * SIGNALS_CREDITS_PER_DOLLAR,  # 100
    Priority.P4.value: 1 * SIGNALS_CREDITS_PER_DOLLAR,  # 100
}


def _judgment_value(content: str, key: str) -> str | None:
    # Parse in Python, not via a SQL `content::jsonb` cast, so one malformed artefact row
    # can't fail the whole cross-team usage report.
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return None
    value = parsed.get(key) if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else None


def _latest_judgment_as_of(
    artefact_type: str,
    key: str,
    cutoff_by_report: dict[uuid.UUID, datetime],
) -> dict[uuid.UUID, str]:
    """For each report, its newest valid `key` from `artefact_type` artefacts at or before the cutoff.

    Walking newest-first and keeping the first valid value per report freezes the judgment at the
    report's chargeable moment, and lets a malformed or too-recent artefact fall through to an
    older valid one. The `-id` tiebreak keeps selection stable when timestamps match.
    """
    rows = (
        SignalReportArtefact.objects.filter(report_id__in=list(cutoff_by_report), type=artefact_type)
        .order_by("report_id", "-created_at", "-id")
        .values_list("report_id", "created_at", "content")
    )
    values: dict[uuid.UUID, str] = {}
    for report_id, created_at, content in rows:
        if report_id in values or created_at > cutoff_by_report[report_id]:
            continue
        value = _judgment_value(content, key)
        if value is not None:
            values[report_id] = value
    return values


def get_signals_billing_credits_by_team(begin: datetime, end: datetime) -> list[tuple[int, int]]:
    """Signals credits used per team in `[begin, end)`.

    A report is billable in this period when the first implementation PR for it appeared in
    the window (no earlier PR run exists), it was `immediately_actionable` as of that PR, and
    it had a priority. Returns `[(team_id, credits), ...]` for teams with non-zero usage only.

    The query is bounded by PRs shipped in the period, not by the total number of reports,
    task runs, or teams: the entry scan uses the `created_at` + `output__pr_url` indexes, and
    every follow-up lookup is keyed on the small resulting report set.
    """
    # Implementation PR runs in this period, with the report's first-PR timestamp (the min, since
    # billable reports have no earlier PR — see the billed_earlier exclusion below). The
    # relationship, pr_url, and team-match constraints stay in one filter() so they resolve
    # against a single bridge join; team_id agreement guards against cross-team mis-attribution.
    report_team: dict[uuid.UUID, int] = {}
    first_pr_at: dict[uuid.UUID, datetime] = {}
    for report_id, team_id, created_at in (
        TaskRun.objects.filter(
            created_at__gte=begin,
            created_at__lt=end,
            output__pr_url__isnull=False,
            task__signal_report_tasks__relationship=_IMPLEMENTATION,
            task__signal_report_tasks__team_id=F("team_id"),
        )
        .exclude(output__pr_url="")
        .values_list("task__signal_report_tasks__report_id", "team_id", "created_at")
    ):
        report_team.setdefault(report_id, team_id)
        if report_id not in first_pr_at or created_at < first_pr_at[report_id]:
            first_pr_at[report_id] = created_at

    if not report_team:
        return []

    report_ids = list(report_team)

    # Exclude reports whose first PR predates this period — they were billed earlier. This is
    # what makes billing idempotent across re-runs and prevents double-charging.
    billed_earlier = set(
        TaskRun.objects.filter(
            created_at__lt=begin,
            output__pr_url__isnull=False,
            task__signal_report_tasks__relationship=_IMPLEMENTATION,
            task__signal_report_tasks__report_id__in=report_ids,
        )
        .exclude(output__pr_url="")
        .values_list("task__signal_report_tasks__report_id", flat=True)
    )
    cutoff_by_report = {
        report_id: first_pr_at[report_id] for report_id in report_ids if report_id not in billed_earlier
    }
    if not cutoff_by_report:
        return []

    artefact_type = SignalReportArtefact.ArtefactType
    priority_by_report = _latest_judgment_as_of(artefact_type.PRIORITY_JUDGMENT, "priority", cutoff_by_report)
    actionability_by_report = _latest_judgment_as_of(
        artefact_type.ACTIONABILITY_JUDGMENT, "actionability", cutoff_by_report
    )

    totals: dict[int, int] = defaultdict(int)
    unpriced = 0
    for report_id in cutoff_by_report:
        if actionability_by_report.get(report_id) != ActionabilityChoice.IMMEDIATELY_ACTIONABLE.value:
            continue
        credits = SIGNALS_PRIORITY_CREDITS.get(priority_by_report.get(report_id))
        if not credits:
            unpriced += 1
            continue
        totals[report_team[report_id]] += credits

    if unpriced:
        logger.warning("signals_billing_unpriced_actionable_reports", count=unpriced, begin=begin, end=end)

    return list(totals.items())
