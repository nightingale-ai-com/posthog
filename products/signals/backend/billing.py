"""Signals billing — credits per actionable report whose implementation produced a PR.

Signals is billed on outcomes, not LLM spend: each report that lands an implementation
PR is charged a flat number of credits based on its priority. The chargeable moment is
deterministic — the `created_at` of the *first* implementation `TaskRun` with a `pr_url`
set — so a report is billed exactly once, in the period that PR first appeared, regardless
of any later status changes.

Credits use the same unit as ai_credits: 1 credit = $0.01.
"""

import uuid
from collections import defaultdict
from datetime import datetime

from django.db.models import CharField, F, Func, JSONField, Value
from django.db.models.functions import Cast

from products.signals.backend.models import SignalReportArtefact, SignalReportTask
from products.tasks.backend.models import TaskRun

_IMPLEMENTATION = SignalReportTask.Relationship.IMPLEMENTATION

SIGNALS_CREDITS_PER_DOLLAR = 100  # 1 credit = $0.01, matching ai_credits

# Flat credits charged per actionable report that shipped a PR, keyed by priority.
SIGNALS_PRIORITY_CREDITS: dict[str, int] = {
    "P0": 24 * SIGNALS_CREDITS_PER_DOLLAR,  # 2400
    "P1": 15 * SIGNALS_CREDITS_PER_DOLLAR,  # 1500
    "P2": 5 * SIGNALS_CREDITS_PER_DOLLAR,  # 500
    "P3": 1 * SIGNALS_CREDITS_PER_DOLLAR,  # 100
    "P4": 1 * SIGNALS_CREDITS_PER_DOLLAR,  # 100
}

_IMMEDIATELY_ACTIONABLE = "immediately_actionable"


def _latest_artefact_values(report_ids: list[uuid.UUID], artefact_type: str, key: str) -> dict[uuid.UUID, str]:
    """Latest value of `key` from each report's most recent artefact of `artefact_type`.

    Priority and actionability live as JSON in `SignalReportArtefact.content`, not as
    columns — mirrors the extraction in `views.py`. Uses the `(report, type)` index.
    """
    if not report_ids:
        return {}

    rows = (
        SignalReportArtefact.objects.filter(
            report_id__in=report_ids,
            type=artefact_type,
            content__startswith="{",
        )
        .order_by("report_id", "-created_at")
        .annotate(
            _val=Func(
                Cast(F("content"), output_field=JSONField()),
                Value(key),
                function="jsonb_extract_path_text",
                output_field=CharField(),
            ),
        )
        .values("report_id", "_val")
        .distinct("report_id")
    )
    return {row["report_id"]: row["_val"] for row in rows if row["_val"]}


def get_signals_billing_credits_by_team(begin: datetime, end: datetime) -> list[tuple[int, int]]:
    """Signals credits used per team in `[begin, end)`.

    A report is billable in this period when the first implementation PR for it appeared in
    the window (no earlier PR run exists), it is `immediately_actionable`, and it has a
    priority. Returns `[(team_id, credits), ...]` for teams with non-zero usage only.

    The query is bounded by PRs shipped in the period, not by the total number of reports,
    task runs, or teams: the entry scan uses the `created_at` + `output__pr_url` indexes, and
    every follow-up lookup is keyed on the small resulting report set.
    """
    # (report, team) pairs whose implementation produced a PR within this period. Distinct
    # collapses multiple runs/tasks for the same report down to one row. The relationship and
    # pr_url constraints stay in a single filter() so they resolve against one bridge join.
    report_team: dict[uuid.UUID, int] = {}
    for report_id, team_id in (
        TaskRun.objects.filter(
            created_at__gte=begin,
            created_at__lt=end,
            output__pr_url__isnull=False,
            task__signal_report_tasks__relationship=_IMPLEMENTATION,
        )
        .exclude(output__pr_url="")
        .values_list("task__signal_report_tasks__report_id", "team_id")
        .distinct()
    ):
        if report_id is not None:
            report_team.setdefault(report_id, team_id)

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
    billable_ids = [report_id for report_id in report_ids if report_id not in billed_earlier]
    if not billable_ids:
        return []

    priority_by_report = _latest_artefact_values(
        billable_ids, SignalReportArtefact.ArtefactType.PRIORITY_JUDGMENT, "priority"
    )
    actionability_by_report = _latest_artefact_values(
        billable_ids, SignalReportArtefact.ArtefactType.ACTIONABILITY_JUDGMENT, "actionability"
    )

    totals: dict[int, int] = defaultdict(int)
    for report_id in billable_ids:
        if actionability_by_report.get(report_id) != _IMMEDIATELY_ACTIONABLE:
            continue
        credits = SIGNALS_PRIORITY_CREDITS.get(priority_by_report.get(report_id) or "")
        if not credits:
            continue
        totals[report_team[report_id]] += credits

    return list(totals.items())
