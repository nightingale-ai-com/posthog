from parameterized import parameterized

from products.web_analytics.dags.web_precompute_window_sizing import (
    MAX_WINDOW_DAYS,
    TARGET_CARDINALITY,
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
