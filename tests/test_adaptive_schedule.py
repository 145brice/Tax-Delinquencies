from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from plan_county_jobs import due_counties, exploration_slot, learned_slot, load_config


class AdaptiveScheduleTests(unittest.TestCase):
    def test_baseline_counties_remain_due_without_inventory(self):
        # 07:17 Eastern on a Monday during daylight-saving time.
        due = due_counties(load_config(), datetime(2026, 9, 14, 11, 17, tzinfo=timezone.utc), [])
        for county in ("duval-fl", "clay-fl", "broward-fl"):
            self.assertIn(county, due)

    def test_learning_selects_productive_hour_after_sufficient_evidence(self):
        start = datetime(2026, 7, 1, 7, tzinfo=timezone.utc)
        runs = []
        for index in range(24):
            local_hour = (7, 10, 13, 16)[index % 4]
            observed = start + timedelta(days=index * 2, hours=local_hour - 7)
            runs.append({"started_at": observed.isoformat(),
                         "added": 10 if local_hour == 13 else 0})
        self.assertEqual(learned_slot("example", "daily", "UTC", runs), 13)

    def test_exploration_slot_is_stable_within_period(self):
        monday = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
        self.assertEqual(exploration_slot("duval-fl", "daily", monday),
                         exploration_slot("duval-fl", "daily", monday + timedelta(days=2)))


if __name__ == "__main__":
    unittest.main()
