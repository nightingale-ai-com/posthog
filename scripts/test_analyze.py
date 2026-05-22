"""Analyze pytest test suite: per-test duration + status, segmented into archetypes.

Inputs (any combination):
  - .test_durations  (pytest-split JSON, repo root) — canonical per-test durations
  - junit XMLs       (CI artifacts) — adds per-shard wall time, setup overhead,
                     status mix, and parametrization data

Output: markdown or self-contained HTML (no external deps).

Usage:
    uv run python scripts/test_analyze.py
    uv run python scripts/test_analyze.py --junit-dir /tmp/testanalyze/run-<id>
    uv run python scripts/test_analyze.py --out logs/test_analysis.md
    uv run python scripts/test_analyze.py --junit-dir <dir> --out logs/test_analysis.html

Once CI starts uploading .testmondata, add a --testmon-db PATH flag and join
per-test file dependencies to surface "covered but slow" and "redundant" archetypes.
"""

from __future__ import annotations

import sys
import html
import json
import argparse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DURATIONS_PATH = REPO_ROOT / ".test_durations"

# pytest-split's .test_durations writes flat defaults (60.0, 18.0) for tests it
# couldn't time properly — newly added tests, flaky reruns where the timer was
# reset, or removed-but-not-pruned entries. These are NOT timeout kills; on
# successful master runs pytest-timeout would have failed the build. Treat them
# as untrustworthy timing.
SUSPECT_DURATIONS = {60.0, 18.0}
SUSPECT_TOLERANCE = 1e-6


# ---- data model -------------------------------------------------------------


@dataclass
class TestRecord:
    nodeid: str
    duration: float
    status: str = "unknown"  # pass | fail | skip | error | unknown

    @property
    def has_suspect_duration(self) -> bool:
        return any(abs(self.duration - v) < SUSPECT_TOLERANCE for v in SUSPECT_DURATIONS)

    @property
    def top_dir(self) -> str:
        return self.nodeid.split("/", 1)[0] if "/" in self.nodeid else ""

    @property
    def module(self) -> str:
        path, _, _ = self.nodeid.partition("::")
        return path

    @property
    def package(self) -> str:
        parts = self.module.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]

    @property
    def class_id(self) -> str:
        """`file.py::Class` or just `file.py` for top-level tests."""
        parts = self.nodeid.split("::")
        return "::".join(parts[:2]) if len(parts) >= 2 else parts[0]

    @property
    def base_name(self) -> str:
        """Strip parametrization brackets: `test_foo[a-1]` -> `test_foo`."""
        nodeid = self.nodeid
        bracket = nodeid.find("[")
        return nodeid[:bracket] if bracket != -1 else nodeid


@dataclass
class ShardRecord:
    """Per-shard wall-time stats from one junit testsuite."""

    name: str
    suite_time: float
    testcase_sum: float
    test_count: int
    pass_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    error_count: int = 0
    hostname: str = ""

    @property
    def overhead(self) -> float:
        return max(0.0, self.suite_time - self.testcase_sum)

    @property
    def overhead_pct(self) -> float:
        return 100 * self.overhead / self.suite_time if self.suite_time else 0


# ---- loading ----------------------------------------------------------------


def load_durations(path: Path) -> dict[str, float]:
    if not path.exists():
        sys.exit(f"missing {path} — run pytest with --store-durations on master first")
    return json.loads(path.read_text())


def parse_junit_dir(junit_dir: Path) -> tuple[dict[str, str], list[ShardRecord]]:
    """Return (status-by-nodeid, list of per-shard records)."""
    if not junit_dir.exists():
        return {}, []
    status_by_nodeid: dict[str, str] = {}
    shards: list[ShardRecord] = []
    for xml_path in sorted(junit_dir.rglob("*.xml")):
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue
        # Use parent dir as the shard label (matches the artifact name).
        shard_label = xml_path.parent.name.replace("junit-results-backend-", "") or xml_path.stem
        for suite in tree.iter("testsuite"):
            tc_sum = 0.0
            passes = fails = skips = errors = 0
            for case in suite.iter("testcase"):
                t = float(case.get("time", 0))
                tc_sum += t
                classname = case.get("classname", "")
                name = case.get("name", "")
                # Build a nodeid candidate that may match .test_durations format.
                # .test_durations uses `path/to/file.py::Class::method`,
                # junit classname is dotted `pkg.mod.Class` — we lose path/dot info,
                # so we store both forms.
                nodeid_dot = f"{classname}::{name}" if classname else name
                if case.find("failure") is not None:
                    status, fails = "fail", fails + 1
                elif case.find("error") is not None:
                    status, errors = "error", errors + 1
                elif case.find("skipped") is not None:
                    status, skips = "skip", skips + 1
                else:
                    status, passes = "pass", passes + 1
                status_by_nodeid[nodeid_dot] = status
            shards.append(
                ShardRecord(
                    name=shard_label,
                    suite_time=float(suite.get("time", 0)),
                    testcase_sum=tc_sum,
                    test_count=int(suite.get("tests", 0)),
                    pass_count=passes,
                    fail_count=fails,
                    skip_count=skips,
                    error_count=errors,
                    hostname=suite.get("hostname", ""),
                )
            )
    return status_by_nodeid, shards


def status_for(nodeid: str, junit_status: dict[str, str]) -> str:
    """Best-effort map of .test_durations nodeid -> junit status.

    Mapping is lossy because junit uses dotted classname while .test_durations
    uses path-form. Returns 'unknown' on no match.
    """
    if not junit_status:
        return "unknown"
    if nodeid in junit_status:
        return junit_status[nodeid]
    module_part, sep, rest = nodeid.partition("::")
    if not sep:
        return "unknown"
    dotted_mod = module_part.replace("/", ".").removesuffix(".py")
    return junit_status.get(f"{dotted_mod}::{rest}", "unknown")


def build_records(durations: dict[str, float], junit_status: dict[str, str]) -> list[TestRecord]:
    return [TestRecord(nodeid=n, duration=d, status=status_for(n, junit_status)) for n, d in durations.items()]


# ---- segmentation -----------------------------------------------------------


@dataclass
class Segment:
    name: str
    description: str
    members: list[TestRecord] = field(default_factory=list)

    @property
    def total_time(self) -> float:
        return sum(r.duration for r in self.members)

    @property
    def count(self) -> int:
        return len(self.members)


def segment_records(records: list[TestRecord]) -> list[Segment]:
    """Initial segmentation using only duration + suspect-duration flag.

    Coarse buckets — meant to surface the most obvious archetypes before
    coverage data is wired in. Each test lands in exactly one segment.
    """
    trusted = sorted(r.duration for r in records if not r.has_suspect_duration and r.duration > 0)
    p95 = trusted[int(len(trusted) * 0.95)]
    p99 = trusted[int(len(trusted) * 0.99)]

    segments = [
        Segment(
            "suspect-duration",
            f"flat default values {sorted(SUSPECT_DURATIONS)} — pytest-split couldn't time these",
        ),
        Segment("slow-outliers", f"> p99 ({p99:.1f}s) — strongest review candidates"),
        Segment("slow-tail", f"p95–p99 ({p95:.2f}s–{p99:.2f}s)"),
        Segment("normal", f"50ms–p95 ({p95:.2f}s)"),
        Segment("fast", "≤ 50ms — near-zero cost"),
    ]
    by_name = {s.name: s for s in segments}

    for r in records:
        if r.has_suspect_duration:
            by_name["suspect-duration"].members.append(r)
        elif r.duration > p99:
            by_name["slow-outliers"].members.append(r)
        elif r.duration > p95:
            by_name["slow-tail"].members.append(r)
        elif r.duration <= 0.050:
            by_name["fast"].members.append(r)
        else:
            by_name["normal"].members.append(r)
    return segments


# ---- shared aggregations ----------------------------------------------------


@dataclass
class Aggregations:
    """Cross-cutting summaries used by both markdown and HTML renderers."""

    total_time: float
    median: float
    p95: float
    p99: float
    max_time: float
    pareto_50: int
    pareto_80: int
    by_package: list[tuple[str, float, int, float]]  # name, total, count, median
    by_class: list[tuple[str, float, int]]  # class_id, total, count
    by_base: list[tuple[str, int, float]]  # base nodeid, param count, total time
    status_counts: Counter[str]


def compute_aggregations(records: list[TestRecord]) -> Aggregations:
    total = sum(r.duration for r in records)
    durs = sorted(r.duration for r in records)
    n = len(durs) or 1
    median_v = durs[n // 2]
    p95 = durs[int(n * 0.95)] if n > 1 else 0
    p99 = durs[int(n * 0.99)] if n > 1 else 0
    cum = 0.0
    p50_n = p80_n = n
    for i, d in enumerate(sorted(durs, reverse=True), 1):
        cum += d
        if cum >= total * 0.5 and p50_n == n:
            p50_n = i
        if cum >= total * 0.8:
            p80_n = i
            break

    by_pkg_raw: dict[str, list[float]] = defaultdict(list)
    by_cls_raw: dict[str, list[float]] = defaultdict(list)
    by_base_raw: dict[str, list[float]] = defaultdict(list)
    for r in records:
        by_pkg_raw[r.package].append(r.duration)
        by_cls_raw[r.class_id].append(r.duration)
        by_base_raw[r.base_name].append(r.duration)

    def _pkg_row(name: str, durs: list[float]) -> tuple[str, float, int, float]:
        sd = sorted(durs)
        return name, sum(durs), len(durs), sd[len(sd) // 2]

    by_package = sorted(
        (_pkg_row(p, ds) for p, ds in by_pkg_raw.items()),
        key=lambda r: -r[1],
    )[:25]
    by_class = sorted(
        ((c, sum(ds), len(ds)) for c, ds in by_cls_raw.items()),
        key=lambda r: -r[1],
    )[:25]
    by_base = sorted(
        ((b, len(ds), sum(ds)) for b, ds in by_base_raw.items() if len(ds) > 1),
        key=lambda r: (-r[1], -r[2]),
    )[:25]

    return Aggregations(
        total_time=total,
        median=median_v,
        p95=p95,
        p99=p99,
        max_time=durs[-1] if durs else 0,
        pareto_50=p50_n,
        pareto_80=p80_n,
        by_package=by_package,
        by_class=by_class,
        by_base=by_base,
        status_counts=Counter(r.status for r in records),
    )


# ---- formatters -------------------------------------------------------------


def _fmt_h(s: float) -> str:
    return f"{s / 3600:.2f}h" if s >= 3600 else f"{s / 60:.1f}m" if s >= 60 else f"{s:.1f}s"


def _fmt_ms(s: float) -> str:
    return f"{s * 1000:.0f}ms" if s < 1 else f"{s:.2f}s"


# ---- markdown report --------------------------------------------------------


def render_markdown(
    records: list[TestRecord],
    segments: list[Segment],
    aggs: Aggregations,
    shards: list[ShardRecord],
) -> str:
    lines: list[str] = []
    lines.append("# Test suite analysis")
    lines.append("")
    lines.append(f"- Tests: **{len(records):,}**")
    lines.append(f"- Total test-time: **{_fmt_h(aggs.total_time)}** (single-threaded)")
    lines.append(
        f"- Distribution: median {_fmt_ms(aggs.median)} · p95 {aggs.p95:.2f}s · "
        f"p99 {aggs.p99:.2f}s · max {aggs.max_time:.1f}s"
    )
    lines.append(
        f"- Pareto: 50% in **{aggs.pareto_50:,}** tests ({100 * aggs.pareto_50 / len(records):.1f}%); "
        f"80% in **{aggs.pareto_80:,}** ({100 * aggs.pareto_80 / len(records):.1f}%)"
    )
    lines.append("")

    if shards:
        wall = sum(s.suite_time for s in shards)
        tc_sum = sum(s.testcase_sum for s in shards)
        overhead = wall - tc_sum
        lines.append("## Setup overhead (junit)")
        lines.append("")
        lines.append(
            f"- Suite wall (all shards): **{_fmt_h(wall)}** · "
            f"Testcase sum: **{_fmt_h(tc_sum)}** · "
            f"**Setup/teardown overhead: {_fmt_h(overhead)} ({100 * overhead / wall:.1f}%)**"
        )
        lines.append("")
        sranked = sorted(shards, key=lambda s: -s.overhead)
        lines.append("### Top 10 shards by setup overhead")
        lines.append("")
        lines.append("| shard | suite | testcase | overhead | overhead % | tests |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for s in sranked[:10]:
            lines.append(
                f"| {s.name} | {s.suite_time:.0f}s | {s.testcase_sum:.0f}s | "
                f"{s.overhead:.0f}s | {s.overhead_pct:.1f}% | {s.test_count} |"
            )
        lines.append("")

    lines.append("## Duration segments")
    lines.append("")
    lines.append("| segment | count | total time | % of suite | description |")
    lines.append("|---|---:|---:|---:|---|")
    for s in segments:
        pct = 100 * s.total_time / aggs.total_time if aggs.total_time else 0
        lines.append(f"| {s.name} | {s.count:,} | {_fmt_h(s.total_time)} | {pct:.1f}% | {s.description} |")
    lines.append("")

    for s in segments:
        if not s.members:
            continue
        lines.append(f"### {s.name} — top 10 by duration")
        lines.append("")
        for r in sorted(s.members, key=lambda x: -x.duration)[:10]:
            tag = "" if r.status in ("pass", "unknown") else f" `[{r.status}]`"
            lines.append(f"- `{r.duration:6.2f}s` {r.nodeid}{tag}")
        lines.append("")

    lines.append("## Hottest packages")
    lines.append("")
    lines.append("| package | tests | total time | mean | median |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, total, count, med in aggs.by_package[:20]:
        lines.append(f"| {name} | {count:,} | {_fmt_h(total)} | {total / count:.2f}s | {_fmt_ms(med)} |")
    lines.append("")

    lines.append("## Slowest classes (file::Class)")
    lines.append("")
    lines.append("| class | tests | total time | mean |")
    lines.append("|---|---:|---:|---:|")
    for cid, total, count in aggs.by_class[:20]:
        lines.append(f"| `{cid}` | {count} | {_fmt_h(total)} | {total / count:.2f}s |")
    lines.append("")

    if aggs.by_base:
        lines.append("## Parametrization explosion (top base tests by param count)")
        lines.append("")
        lines.append("| base test | param count | total time |")
        lines.append("|---|---:|---:|")
        for base, n, total in aggs.by_base[:20]:
            lines.append(f"| `{base}` | {n} | {_fmt_h(total)} |")
        lines.append("")

    if aggs.status_counts and set(aggs.status_counts) - {"unknown"}:
        lines.append("## Status mix (from junit)")
        lines.append("")
        for st, n in aggs.status_counts.most_common():
            lines.append(f"- {st}: {n:,}")
        lines.append("")

    return "\n".join(lines)


# ---- HTML report ------------------------------------------------------------

CSS = """
:root { --fg:#0f172a; --muted:#64748b; --bg:#f8fafc; --card:#fff; --line:#e2e8f0;
        --accent:#0ea5e9; --warn:#dc2626; --ok:#16a34a; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif;
       color: var(--fg); background: var(--bg); margin: 0; padding: 24px; }
.container { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 12px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
h3 { font-size: 14px; margin: 16px 0 6px; color: var(--muted); font-weight: 600; }
.subtitle { color: var(--muted); margin: 0 0 24px; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0 24px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; }
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
.card .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
.card.warn .value { color: var(--warn); }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line);
        border-radius: 6px; overflow: hidden; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { background: #f1f5f9; font-weight: 600; color: var(--fg); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: 0; }
code { font: 12px/1.3 'SF Mono', 'Monaco', monospace; background: #f1f5f9; padding: 1px 4px; border-radius: 3px; }
.path { font: 12px/1.3 'SF Mono', 'Monaco', monospace; color: var(--fg); }
.badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; }
.badge.pass { background: #dcfce7; color: var(--ok); }
.badge.fail, .badge.error { background: #fee2e2; color: var(--warn); }
.badge.skip { background: #fef3c7; color: #b45309; }
.badge.suspect { background: #fce7f3; color: #be185d; }
details > summary { cursor: pointer; padding: 6px 0; color: var(--accent); font-size: 13px; }
.bar-row { display: flex; align-items: center; gap: 8px; padding: 2px 0; font-size: 12px; }
.bar-row .label { flex: 0 0 180px; font: 11px/1.3 'SF Mono', monospace; overflow: hidden; text-overflow: ellipsis; }
.bar-row .bar { flex: 1; height: 14px; background: #f1f5f9; border-radius: 2px; position: relative; overflow: hidden; }
.bar-row .bar .testcase { background: var(--accent); height: 100%; position: absolute; left: 0; top: 0; }
.bar-row .bar .overhead { background: #fca5a5; height: 100%; position: absolute; top: 0; }
.bar-row .value { flex: 0 0 110px; text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.legend { display: flex; gap: 16px; margin: 8px 0 12px; font-size: 12px; color: var(--muted); }
.legend .sw { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: middle; margin-right: 4px; }
.legend .sw.testcase { background: var(--accent); }
.legend .sw.overhead { background: #fca5a5; }
.footnote { color: var(--muted); font-size: 12px; margin-top: 8px; }
"""


def _h(s: str | float) -> str:
    return html.escape(str(s))


def _card(label: str, value: str, sub: str = "", warn: bool = False) -> str:
    cls = "card warn" if warn else "card"
    return (
        f'<div class="{cls}"><div class="label">{_h(label)}</div>'
        f'<div class="value">{_h(value)}</div>'
        f'<div class="sub">{_h(sub)}</div></div>'
    )


def _status_badge(status: str) -> str:
    if status in ("pass", "fail", "error", "skip"):
        return f'<span class="badge {status}">{status}</span>'
    return ""


def _shard_bars(shards: list[ShardRecord]) -> str:
    if not shards:
        return ""
    max_time = max(s.suite_time for s in shards)
    rows: list[str] = []
    for s in sorted(shards, key=lambda x: -x.suite_time):
        tc_pct = 100 * s.testcase_sum / max_time
        ov_left_pct = tc_pct
        ov_pct = 100 * s.overhead / max_time
        rows.append(
            f'<div class="bar-row">'
            f'<div class="label">{_h(s.name)}</div>'
            f'<div class="bar">'
            f'<div class="testcase" style="width:{tc_pct:.2f}%"></div>'
            f'<div class="overhead" style="left:{ov_left_pct:.2f}%;width:{ov_pct:.2f}%"></div>'
            f"</div>"
            f'<div class="value">{s.suite_time:.0f}s · {s.test_count} tests</div>'
            f"</div>"
        )
    return "\n".join(rows)


def render_html(
    records: list[TestRecord],
    segments: list[Segment],
    aggs: Aggregations,
    shards: list[ShardRecord],
) -> str:
    total = aggs.total_time
    parts: list[str] = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append("<title>Test suite analysis</title>")
    parts.append(f"<style>{CSS}</style></head><body><div class='container'>")
    parts.append("<h1>Test suite analysis</h1>")
    parts.append(
        f"<p class='subtitle'>{len(records):,} tests · "
        f"single-threaded test-time {_fmt_h(total)} · "
        f"median {_fmt_ms(aggs.median)}, p95 {aggs.p95:.2f}s, p99 {aggs.p99:.2f}s, "
        f"max {aggs.max_time:.1f}s</p>"
    )

    # Headline cards.
    cards = [
        _card("Tests", f"{len(records):,}"),
        _card("Total test-time", _fmt_h(total), "single-threaded"),
        _card("50% of time in", f"{aggs.pareto_50:,} tests", f"{100 * aggs.pareto_50 / len(records):.1f}% of suite"),
        _card("80% of time in", f"{aggs.pareto_80:,} tests", f"{100 * aggs.pareto_80 / len(records):.1f}% of suite"),
    ]
    if shards:
        wall = sum(s.suite_time for s in shards)
        tc_sum = sum(s.testcase_sum for s in shards)
        overhead = wall - tc_sum
        cards.extend(
            [
                _card("CI wall (sum)", _fmt_h(wall), f"{len(shards)} shards"),
                _card("Testcase sum", _fmt_h(tc_sum), f"{100 * tc_sum / wall:.1f}% of wall"),
                _card(
                    "Setup overhead",
                    _fmt_h(overhead),
                    f"{100 * overhead / wall:.1f}% of wall",
                    warn=overhead / wall > 0.5,
                ),
                _card(
                    "Slowest/fastest shard",
                    f"{max(s.suite_time for s in shards) / min(s.suite_time for s in shards):.1f}×",
                    f"{min(s.suite_time for s in shards):.0f}s – {max(s.suite_time for s in shards):.0f}s",
                ),
            ]
        )
    parts.append(f"<div class='cards'>{''.join(cards)}</div>")

    # Shard balance.
    if shards:
        parts.append("<h2>Shard balance &amp; setup overhead</h2>")
        parts.append(
            "<div class='legend'>"
            "<span><span class='sw testcase'></span>Testcase time</span>"
            "<span><span class='sw overhead'></span>Setup / teardown / fixtures</span>"
            "</div>"
        )
        parts.append(_shard_bars(shards))
        parts.append(
            "<p class='footnote'>Each bar is one shard. Width = suite wall time. "
            "Blue = sum of testcase times; pink = the gap (fixtures, DB migrations, "
            "container startup, teardown). High pink % means the shard is dominated "
            "by setup, not test execution.</p>"
        )
        # Detailed table for top-overhead shards
        parts.append("<h3>Top shards by setup overhead</h3>")
        parts.append(
            "<table><thead><tr><th>Shard</th>"
            "<th class='num'>Suite wall</th><th class='num'>Testcase sum</th>"
            "<th class='num'>Overhead</th><th class='num'>Overhead %</th>"
            "<th class='num'>Tests</th><th class='num'>Skips</th></tr></thead><tbody>"
        )
        for s in sorted(shards, key=lambda x: -x.overhead)[:15]:
            parts.append(
                f"<tr><td class='path'>{_h(s.name)}</td>"
                f"<td class='num'>{s.suite_time:.0f}s</td>"
                f"<td class='num'>{s.testcase_sum:.0f}s</td>"
                f"<td class='num'>{s.overhead:.0f}s</td>"
                f"<td class='num'>{s.overhead_pct:.1f}%</td>"
                f"<td class='num'>{s.test_count}</td>"
                f"<td class='num'>{s.skip_count}</td></tr>"
            )
        parts.append("</tbody></table>")

    # Duration segments.
    parts.append("<h2>Duration segments</h2>")
    parts.append(
        "<table><thead><tr><th>Segment</th><th class='num'>Tests</th>"
        "<th class='num'>Total time</th><th class='num'>% of suite</th>"
        "<th>Description</th></tr></thead><tbody>"
    )
    for s in segments:
        pct = 100 * s.total_time / total if total else 0
        warn_cls = " class='badge suspect'" if s.name == "suspect-duration" else ""
        parts.append(
            f"<tr><td><span{warn_cls}>{_h(s.name)}</span></td>"
            f"<td class='num'>{s.count:,}</td>"
            f"<td class='num'>{_fmt_h(s.total_time)}</td>"
            f"<td class='num'>{pct:.1f}%</td>"
            f"<td>{_h(s.description)}</td></tr>"
        )
    parts.append("</tbody></table>")

    for s in segments:
        if not s.members or s.name == "fast":
            continue
        parts.append(f"<details><summary>{_h(s.name)} — top 25</summary><table>")
        parts.append("<thead><tr><th class='num'>Duration</th><th>Test</th><th>Status</th></tr></thead><tbody>")
        for r in sorted(s.members, key=lambda x: -x.duration)[:25]:
            parts.append(
                f"<tr><td class='num'>{r.duration:.2f}s</td>"
                f"<td class='path'>{_h(r.nodeid)}</td>"
                f"<td>{_status_badge(r.status)}</td></tr>"
            )
        parts.append("</tbody></table></details>")

    # Hottest packages.
    parts.append("<h2>Hottest packages</h2>")
    parts.append(
        "<table><thead><tr><th>Package</th><th class='num'>Tests</th>"
        "<th class='num'>Total</th><th class='num'>Mean</th>"
        "<th class='num'>Median</th></tr></thead><tbody>"
    )
    for name, total_t, count, med in aggs.by_package[:25]:
        parts.append(
            f"<tr><td class='path'>{_h(name)}</td>"
            f"<td class='num'>{count:,}</td>"
            f"<td class='num'>{_fmt_h(total_t)}</td>"
            f"<td class='num'>{total_t / count:.2f}s</td>"
            f"<td class='num'>{_fmt_ms(med)}</td></tr>"
        )
    parts.append("</tbody></table>")

    # Slowest classes.
    parts.append("<h2>Slowest classes</h2>")
    parts.append(
        "<table><thead><tr><th>Class</th><th class='num'>Tests</th>"
        "<th class='num'>Total</th><th class='num'>Mean</th></tr></thead><tbody>"
    )
    for cid, total_t, count in aggs.by_class[:25]:
        parts.append(
            f"<tr><td class='path'>{_h(cid)}</td>"
            f"<td class='num'>{count}</td>"
            f"<td class='num'>{_fmt_h(total_t)}</td>"
            f"<td class='num'>{total_t / count:.2f}s</td></tr>"
        )
    parts.append("</tbody></table>")

    # Parametrization explosion.
    if aggs.by_base:
        parts.append("<h2>Parametrization explosion</h2>")
        parts.append(
            "<p class='footnote'>Base tests (without <code>[param]</code> suffix) "
            "with the most parameter variants. Many variants × non-trivial time per "
            "variant is a strong pruning candidate — most parametrized tests have "
            "diminishing fault-detection value past a handful of cases.</p>"
        )
        parts.append(
            "<table><thead><tr><th>Base test</th><th class='num'>Param count</th>"
            "<th class='num'>Total time</th><th class='num'>Mean per param</th></tr></thead><tbody>"
        )
        for base, n, total_t in aggs.by_base[:25]:
            parts.append(
                f"<tr><td class='path'>{_h(base)}</td>"
                f"<td class='num'>{n}</td>"
                f"<td class='num'>{_fmt_h(total_t)}</td>"
                f"<td class='num'>{total_t / n:.2f}s</td></tr>"
            )
        parts.append("</tbody></table>")

    # Status mix.
    if aggs.status_counts and set(aggs.status_counts) - {"unknown"}:
        parts.append("<h2>Status mix (from junit)</h2>")
        parts.append("<table><thead><tr><th>Status</th><th class='num'>Count</th></tr></thead><tbody>")
        for st, n in aggs.status_counts.most_common():
            parts.append(f"<tr><td>{_status_badge(st) or _h(st)}</td><td class='num'>{n:,}</td></tr>")
        parts.append("</tbody></table>")

    parts.append("</div></body></html>")
    return "".join(parts)


# ---- entrypoint -------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--durations", type=Path, default=DURATIONS_PATH)
    parser.add_argument(
        "--junit-dir",
        type=Path,
        help="Directory tree of junit XMLs (CI artifact download). Enables shard/overhead analysis.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Write report here. Extension picks format: .html (rich), .md (default).",
    )
    args = parser.parse_args()

    durations = load_durations(args.durations)
    junit_status, shards = parse_junit_dir(args.junit_dir) if args.junit_dir else ({}, [])
    records = build_records(durations, junit_status)
    segments = segment_records(records)
    aggs = compute_aggregations(records)

    fmt = "html" if (args.out and args.out.suffix.lower() in {".html", ".htm"}) else "md"
    render = render_html if fmt == "html" else render_markdown
    report = render(records, segments, aggs, shards)

    if args.out:
        args.out.write_text(report)
        sys.stderr.write(f"wrote {args.out} ({fmt})\n")
    else:
        sys.stdout.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
