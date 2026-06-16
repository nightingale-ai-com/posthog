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
from products.signals.backend.report_generation.research import ActionabilityChoice, Priority
from products.tasks.backend.models import TaskRun

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


def _artefact_json_value(key: str) -> Func:
    """Postgres expression extracting `content->>key` from a `SignalReportArtefact`."""
    return Func(
        Cast(F("content"), output_field=JSONField()),
        Value(key),
        function="jsonb_extract_path_text",
        output_field=CharField(),
    )


def _latest_priority_and_actionability(
    report_ids: list[uuid.UUID],
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """Latest priority and actionability per report, from their newest judgment artefacts.

    Both values live as JSON in `SignalReportArtefact.content`, not as columns. A single query
    over the `(report, type)` index fetches both, picking the newest artefact per (report, type).
    """
    if not report_ids:
        return {}, {}

    artefact_type = SignalReportArtefact.ArtefactType
    rows = (
        SignalReportArtefact.objects.filter(
            report_id__in=report_ids,
            type__in=[artefact_type.PRIORITY_JUDGMENT, artefact_type.ACTIONABILITY_JUDGMENT],
            content__startswith="{",
        )
        .order_by("report_id", "type", "-created_at")
        .annotate(_priority=_artefact_json_value("priority"), _actionability=_artefact_json_value("actionability"))
        .values("report_id", "type", "_priority", "_actionability")
        .distinct("report_id", "type")
    )

    priority: dict[uuid.UUID, str] = {}
    actionability: dict[uuid.UUID, str] = {}
    for row in rows:
        if row["type"] == artefact_type.PRIORITY_JUDGMENT:
            if row["_priority"]:
                priority[row["report_id"]] = row["_priority"]
        elif row["_actionability"]:
            actionability[row["report_id"]] = row["_actionability"]
    return priority, actionability


def get_signals_billing_credits_by_team(begin: datetime, end: datetime) -> list[tuple[int, int]]:
    """Signals credits used per team in `[begin, end)`.

    A report is billable in this period when the first implementation PR for it appeared in
    the window (no earlier PR run exists), it is `immediately_actionable`, and it has a
    priority. Returns `[(team_id, credits), ...]` for teams with non-zero usage only.

    The query is bounded by PRs shipped in the period, not by the total number of reports,
    task runs, or teams: the entry scan uses the `created_at` + `output__pr_url` indexes, and
    every follow-up lookup is keyed on the small resulting report set.
    """
    # (report -> team) for reports whose implementation produced a PR within this period.
    # Distinct collapses multiple runs/tasks for the same report; the relationship and pr_url
    # constraints stay in a single filter() so they resolve against one bridge join.
    report_team: dict[uuid.UUID, int] = dict(
        TaskRun.objects.filter(
            created_at__gte=begin,
            created_at__lt=end,
            output__pr_url__isnull=False,
            task__signal_report_tasks__relationship=_IMPLEMENTATION,
        )
        .exclude(output__pr_url="")
        .values_list("task__signal_report_tasks__report_id", "team_id")
        .distinct()
    )

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

    priority_by_report, actionability_by_report = _latest_priority_and_actionability(billable_ids)

    totals: dict[int, int] = defaultdict(int)
    for report_id in billable_ids:
        if actionability_by_report.get(report_id) != ActionabilityChoice.IMMEDIATELY_ACTIONABLE.value:
            continue
        credits = SIGNALS_PRIORITY_CREDITS.get(priority_by_report.get(report_id))
        if not credits:
            continue
        totals[report_team[report_id]] += credits

    return list(totals.items())
