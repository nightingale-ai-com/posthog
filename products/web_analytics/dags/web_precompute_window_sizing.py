"""Daily per-team insert-window sizing for lazy precompute.

Very high-cardinality teams' wide back-window `GROUP BY (session, breakdown)` OOMs the
precompute INSERT. This job computes the largest window (in days) each heavy team's data
fits within a memory budget — `window = clamp(TARGET / peak_daily_cardinality, 1, 7)` —
and materializes `{team_id: window_days}` into Redis. The web-analytics lazy precompute
runners read it (via `get_team_max_window_days`) and pass it to
`ensure_precomputed(max_window_days=...)`, capping those teams' jobs so the OOM never
happens. Teams without an entry use the default TTL-merged window.
"""

import dagster

from posthog.schema import ProductKey

from posthog import redis
from posthog.clickhouse import query_tagging
from posthog.clickhouse.client import sync_execute
from posthog.clickhouse.query_tagging import Feature, tags_context
from posthog.dags.common import JobOwners, dagster_tags

from products.web_analytics.backend.hogql_queries.web_lazy_precompute_common import TEAM_WINDOW_DAYS_REDIS_KEY

# Target GROUP BY cardinality per job — at the calibrated ~1.67 GiB/M-row slope this is
# ~16 GiB, leaving headroom under the per-query cap for concurrent inserts.
TARGET_CARDINALITY = 5_000_000
MAX_WINDOW_DAYS = 7
# Only teams whose peak day would blow the budget at the max window need an entry.
CARDINALITY_FLOOR = TARGET_CARDINALITY // MAX_WINDOW_DAYS
# Size on the busiest recent day (conservative), not a single possibly-quiet day.
LOOKBACK_DAYS = 3
# Fail-safe: a stale set expires if the job stops running, so the executor reverts to
# the default window rather than a frozen one.
REDIS_TTL_SECONDS = 2 * 24 * 60 * 60


def window_for_cardinality(peak_daily_card: int) -> int:
    return max(1, min(MAX_WINDOW_DAYS, TARGET_CARDINALITY // max(peak_daily_card, 1)))


def store_team_windows(rows: list[tuple[int, int]]) -> int:
    """Overwrite the Redis hash from (team_id, peak_daily_card) rows; return the count stored.

    Only teams needing a sub-default window are stored; the rest fall back to the default.
    The whole set is rewritten under a transactional pipeline so a concurrent reader sees the
    old or new hash, never a half-written one, and teams that dropped below threshold are cleared.
    """
    windows = {
        str(int(team_id)): str(window_for_cardinality(int(peak)))
        for team_id, peak in rows
        if window_for_cardinality(int(peak)) < MAX_WINDOW_DAYS
    }

    client = redis.get_client()
    pipe = client.pipeline(transaction=True)
    pipe.delete(TEAM_WINDOW_DAYS_REDIS_KEY)
    if windows:
        pipe.hset(TEAM_WINDOW_DAYS_REDIS_KEY, mapping=windows)
        pipe.expire(TEAM_WINDOW_DAYS_REDIS_KEY, REDIS_TTL_SECONDS)
    pipe.execute()
    return len(windows)


@dagster.op
def materialize_team_windows_op(context: dagster.OpExecutionContext) -> None:
    # Cardinality proxy: (session, $pathname) over $pageview/$screen. This is the GROUP BY
    # key for the path runners and a conservative upper bound for the others (stats/goals
    # break down on lower-cardinality keys), so the resulting cap over-narrows rather than
    # under-narrows for them; `spill_to_disk` on the inserts backstops any under-sizing.
    query_tagging.get_query_tags().with_dagster(dagster_tags(context))
    with tags_context(
        product=ProductKey.WEB_ANALYTICS, feature=Feature.PREAGGREGATION, query_type="web_precompute_window_sizing"
    ):
        rows = sync_execute(
            """
            SELECT team_id, max(daily_card) AS peak_daily_card
            FROM (
                SELECT
                    team_id,
                    toDate(timestamp) AS d,
                    uniqHLL12((`$session_id`, nullIf(nullIf(`mat_$pathname`, ''), 'null'))) AS daily_card
                FROM events
                WHERE event IN ('$pageview', '$screen')
                    AND timestamp >= now() - toIntervalDay(%(lookback)s)
                GROUP BY team_id, d
            )
            GROUP BY team_id
            HAVING peak_daily_card > %(floor)s
            """,
            {"lookback": LOOKBACK_DAYS, "floor": CARDINALITY_FLOOR},
        )

    count = store_team_windows(rows)
    context.log.info(f"materialized {count} per-team insert-window caps")
    context.add_output_metadata({"team_count": count})


@dagster.job(tags={"owner": JobOwners.TEAM_WEB_ANALYTICS.value})
def web_precompute_window_sizing_job():
    materialize_team_windows_op()


@dagster.schedule(
    cron_schedule="0 6 * * *",  # daily, 06:00 UTC — cardinality moves on a daily granularity
    job=web_precompute_window_sizing_job,
    execution_timezone="UTC",
    tags={"owner": JobOwners.TEAM_WEB_ANALYTICS.value},
)
def web_precompute_window_sizing_schedule(context: dagster.ScheduleEvaluationContext):
    return dagster.RunRequest()
