#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import budget


class TestBudgetBot(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        budget.BUDGET_DIR = self.tmp_path / "budget"
        budget.BUDGET_HISTORY = budget.BUDGET_DIR / "history.jsonl"
        budget.BUDGET_META = budget.BUDGET_DIR / "meta.json"
        budget.QUOTA_FILE = self.tmp_path / "quota.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_estimate_task_within_limits(self):
        task = {"id": "VIK-100", "type": "code", "risk": "low", "scope": "agents/budget.py"}
        est = budget.estimate_task(task, provider="antigravity")
        self.assertTrue(est["allowed"])
        self.assertGreater(est["projected_total_tokens"], 0)
        self.assertGreater(est["projected_cost_usd"], 0.0)

    def test_estimate_task_huge_scope_challenge(self):
        # 30 files in scope should challenge tokens
        scope = " ".join([f"file_{i}.py" for i in range(30)])
        task = {"id": "VIK-101", "type": "code", "risk": "low", "scope": scope}
        est = budget.estimate_task(task, provider="codex")
        self.assertFalse(est["allowed"])
        self.assertTrue(any("Tokens proyectados" in r for r in est["reasons"]))

    def test_record_run_and_comment_format(self):
        entry = budget.record_run("VIK-102", "run_1", "codex", 15000, 2000, 45.0)
        self.assertEqual(entry["task_id"], "VIK-102")
        self.assertEqual(entry["total_tokens"], 17000)

        budget.record_run("VIK-102", "run_2", "claude", 10000, 1000, 30.0)
        comment = budget.format_task_cost_comment("VIK-102")
        self.assertIn("28,000 tokens", comment)
        self.assertIn("claude, codex", comment)

    def test_check_burn_rate_alerts(self):
        now = 1000000.0
        # Simular cuota de 5h agotada y semanal con quema acelerada
        quota_data = {
            "readings": [
                {
                    "group": "claude",
                    "window": "5h",
                    "used": 100.0,
                    "reset": now + 3600.0,
                    "read": now,
                },
                {
                    "group": "codex",
                    "window": "semanal",
                    "used": 80.0,
                    "reset": now + 72 * 3600.0,  # faltan 72h de 168h, usado 80% en 57% del tiempo -> burn_rate > 1.4
                    "read": now,
                },
            ]
        }
        budget.QUOTA_FILE.write_text(json.dumps(quota_data))

        with patch("time.time", return_value=now), patch("budget.notify_telegram", return_value=True):
            alerts = budget.check_burn_rate(notify=True)
            self.assertEqual(len(alerts), 2)
            self.assertEqual(alerts[0]["level"], "CRITICAL")
            self.assertTrue(alerts[0]["notified"])
            self.assertEqual(alerts[1]["level"], "CRITICAL")
            self.assertTrue(alerts[1]["notified"])


if __name__ == "__main__":
    unittest.main()
