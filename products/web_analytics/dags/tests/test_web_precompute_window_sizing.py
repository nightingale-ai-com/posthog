from parameterized import parameterized

from posthog import redis

from products.web_analytics.backend.hogql_queries.web_lazy_precompute_common import TEAM_WINDOW_DAYS_REDIS_KEY
from products.web_analytics.dags.web_precompute_window_sizing import (
    CARDINALITY_FLOOR,
    MAX_WINDOW_DAYS,
    REDIS_TTL_SECONDS,
    TARGET_CARDINALITY,
    store_team_windows,
    window_for_cardinality,
)


class TestWindowForCardinality:
    @parameterized.expand(
        [
            ("over_budget_even_at_one_day", 40_000_000, 1),
            ("at_target", 5_000_000, 1),
            ("half_target", 2_500_000, 2),
            ("third_target", 1_650_000, 3),
            ("just_at_floor", 700_000, 7),
            ("tiny", 10_000, 7),
            ("zero_guarded", 0, 7),
        ]
    )
    def test_window_for_cardinality(self, _name, peak_daily_card, expected_window):
        assert window_for_cardinality(peak_daily_card) == expected_window

    def test_result_always_in_bounds(self):
        for card in (1, 10**3, 10**5, 10**6, 10**7, 10**9):
            assert 1 <= window_for_cardinality(card) <= MAX_WINDOW_DAYS

    def test_constants_sane(self):
        assert TARGET_CARDINALITY > 0
        assert MAX_WINDOW_DAYS == 7


class TestStoreTeamWindows:
    def teardown_method(self):
        redis.get_client().delete(TEAM_WINDOW_DAYS_REDIS_KEY)

    def test_stores_only_sub_default_windows_and_sets_ttl(self):
        client = redis.get_client()
        client.delete(TEAM_WINDOW_DAYS_REDIS_KEY)
        # team 1: needs a 2-day window (stored); team 2: half-target → would be 7d == default (not stored)
        count = store_team_windows([(1, 2_500_000), (2, CARDINALITY_FLOOR)])
        assert count == 1
        assert client.hget(TEAM_WINDOW_DAYS_REDIS_KEY, "1") == b"2"
        assert client.hget(TEAM_WINDOW_DAYS_REDIS_KEY, "2") is None
        ttl = client.ttl(TEAM_WINDOW_DAYS_REDIS_KEY)
        assert 0 < ttl <= REDIS_TTL_SECONDS

    def test_rewrite_clears_team_that_dropped_below_threshold(self):
        client = redis.get_client()
        client.delete(TEAM_WINDOW_DAYS_REDIS_KEY)
        store_team_windows([(1, 2_500_000)])
        assert client.hget(TEAM_WINDOW_DAYS_REDIS_KEY, "1") == b"2"
        # team 1 no longer over the floor → the overwrite must drop it
        count = store_team_windows([(2, 2_500_000)])
        assert count == 1
        assert client.hget(TEAM_WINDOW_DAYS_REDIS_KEY, "1") is None
        assert client.hget(TEAM_WINDOW_DAYS_REDIS_KEY, "2") == b"2"

    def test_empty_result_removes_key(self):
        client = redis.get_client()
        store_team_windows([(1, 2_500_000)])
        assert client.exists(TEAM_WINDOW_DAYS_REDIS_KEY)
        count = store_team_windows([])
        assert count == 0
        assert not client.exists(TEAM_WINDOW_DAYS_REDIS_KEY)
