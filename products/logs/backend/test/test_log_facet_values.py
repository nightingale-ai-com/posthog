import os
import json

from posthog.test.base import APIBaseTest, ClickhouseTestMixin

from rest_framework import status

from posthog.clickhouse.client import sync_execute


class TestLogFacetValues(ClickhouseTestMixin, APIBaseTest):
    CLASS_DATA_LEVEL_SETUP = True

    DATE_RANGE = {"date_from": "2025-12-16T09:00:00Z", "date_to": "2025-12-16T11:00:00Z"}

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        with open(os.path.join(os.path.dirname(__file__), "test_logs.jsonnd")) as f:
            sql = ""
            for line in f:
                log_item = json.loads(line)
                log_item["team_id"] = cls.team.id
                sql += json.dumps(log_item) + "\n"
            sync_execute(f"""
                INSERT INTO logs
                FORMAT JSONEachRow
                {sql}
            """)

    def _facet(self, facet_field: str, **filters) -> dict[str, int]:
        body = {"query": {"facetField": facet_field, "dateRange": self.DATE_RANGE, **filters}}
        response = self.client.post(f"/api/projects/{self.team.pk}/logs/facet_values", body, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {r["value"]: r["count"] for r in response.json()["results"]}

    def test_severity_facet_ignores_its_own_severity_filter(self):
        """Selecting a level must NOT change the Level facet's own counts (cross-filtering)."""
        base = self._facet("severity_text")
        self.assertGreater(len(base), 0)

        a_level = next(iter(base))
        filtered = self._facet("severity_text", severityLevels=[a_level])
        self.assertEqual(filtered, base, "severity facet must ignore the severityLevels filter")

    def test_severity_facet_honors_service_filter(self):
        """A service selection DOES re-scope the Level facet's counts."""
        base = self._facet("severity_text")
        a_service = next(iter(self._facet("service_name")))

        scoped = self._facet("severity_text", serviceNames=[a_service])
        self.assertLess(
            sum(scoped.values()), sum(base.values()), "filtering by one service should reduce the level totals"
        )

    def test_service_facet_ignores_its_own_service_filter(self):
        """Selecting a service must NOT change the Service facet's own counts."""
        base = self._facet("service_name")
        self.assertGreater(len(base), 0)

        a_service = next(iter(base))
        filtered = self._facet("service_name", serviceNames=[a_service])
        self.assertEqual(filtered, base, "service facet must ignore the serviceNames filter")

    def test_service_facet_honors_severity_filter(self):
        """A level selection DOES re-scope the Service facet's counts."""
        base = self._facet("service_name")
        a_level = next(iter(self._facet("severity_text")))

        scoped = self._facet("service_name", severityLevels=[a_level])
        self.assertLessEqual(sum(scoped.values()), sum(base.values()))

    def test_invalid_facet_field_is_rejected(self):
        body = {"query": {"facetField": "body", "dateRange": self.DATE_RANGE}}
        response = self.client.post(f"/api/projects/{self.team.pk}/logs/facet_values", body, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
