#!/usr/bin/env python3
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "orchestrator.py"


class OrchestratorTest(unittest.TestCase):
    def cli(self, state, *args, ok=True):
        result = subprocess.run(["python3", str(CLI), "--state", str(state), *args], text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_high_risk_needs_two_cross_reviews_and_human(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.cli(state, "add-task", "VIK-1", "--title", "POC", "--component", "agents", "--type", "code", "--risk", "high")
            run_id = self.cli(state, "start-run", "VIK-1", "--provider", "codex", "--role", "implementer", "--session-ref", "opaque-1", "--branch", "codex/dev/VIK-1", "--worktree", "/tmp/vik-1").stdout.strip()
            self.cli(state, "submit-run", run_id, "--summary", "sin evidencia", ok=False)
            self.cli(state, "add-evidence", run_id, "--kind", "commit", "--value", "abc123")
            self.cli(state, "add-evidence", run_id, "--kind", "tests", "--value", "2 OK")
            self.cli(state, "add-evidence", run_id, "--kind", "summary", "--value", "POC lista")
            self.cli(state, "add-evidence", run_id, "--kind", "rollback", "--value", "git revert")
            self.cli(state, "submit-run", run_id, "--summary", "POC lista")
            self.assertEqual(json.loads(state.read_text())["tasks"]["VIK-1"]["state"], "in_review")
            for provider in ("gemini", "claude"):
                rid = self.cli(state, "start-run", "VIK-1", "--provider", provider, "--role", "reviewer", "--session-ref", "r", "--branch", "b", "--worktree", "/tmp/r").stdout.strip()
                self.cli(state, "submit-review", rid, "--verdict", "approve", "--summary", "ok")
            data = json.loads(state.read_text())
            self.assertEqual(data["tasks"]["VIK-1"]["state"], "human_review")
            self.cli(state, "decide", "VIK-1", "--human", "gus", "--note", "ok", "--approve")
            self.cli(state, "verify")
            self.cli(state, "complete", "VIK-1")
            self.assertEqual(json.loads(state.read_text())["tasks"]["VIK-1"]["state"], "done")

    def test_researcher_cannot_run_code_task(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.cli(state, "add-task", "VIK-2", "--title", "Code", "--component", "agents", "--type", "code", "--risk", "low")
            self.cli(state, "start-run", "VIK-2", "--provider", "claude", "--role", "researcher", "--session-ref", "opaque", "--branch", "claude/research/VIK-2", "--worktree", "/tmp/vik-2", ok=False)

    def implement(self, state, task, risk="low"):
        self.cli(state, "add-task", task, "--title", "T", "--component", "agents", "--type", "code", "--risk", risk)
        rid = self.cli(state, "start-run", task, "--provider", "codex", "--role", "implementer", "--session-ref", "s", "--branch", "b", "--worktree", "/tmp/w").stdout.strip()
        for kind in ("commit", "tests", "summary"):
            self.cli(state, "add-evidence", rid, "--kind", kind, "--value", "x")
        self.cli(state, "submit-run", rid, "--summary", "hecho")

    def start_review(self, state, task, provider, ok=True):
        return self.cli(state, "start-run", task, "--provider", provider, "--role", "reviewer", "--session-ref", "r", "--branch", "b", "--worktree", "/tmp/r", ok=ok).stdout.strip()

    def test_nobody_reviews_own_work_and_low_risk_skips_human(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.implement(state, "VIK-3")
            self.start_review(state, "VIK-3", "codex", ok=False)   # el implementador no se revisa
            rid = self.start_review(state, "VIK-3", "gemini")
            self.cli(state, "submit-review", rid, "--verdict", "approve", "--summary", "ok")
            self.assertEqual(json.loads(state.read_text())["tasks"]["VIK-3"]["state"], "validated")
            self.cli(state, "complete", "VIK-3")

    def test_changes_loop_then_escalates_after_max_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.implement(state, "VIK-4")
            for _ in range(3):
                rid = self.start_review(state, "VIK-4", "gemini")
                self.cli(state, "submit-review", rid, "--verdict", "changes", "--summary", "falta X")
                task = json.loads(state.read_text())["tasks"]["VIK-4"]
                if task["state"] == "changes_requested":
                    rid = self.cli(state, "start-run", "VIK-4", "--provider", "codex", "--role", "implementer", "--session-ref", "s", "--branch", "b", "--worktree", "/tmp/w").stdout.strip()
                    for kind in ("commit", "tests", "summary"):
                        self.cli(state, "add-evidence", rid, "--kind", kind, "--value", "x")
                    self.cli(state, "submit-run", rid, "--summary", "v2")
            self.assertEqual(json.loads(state.read_text())["tasks"]["VIK-4"]["state"], "human_review")
            self.cli(state, "decide", "VIK-4", "--human", "gus", "--note", "probá otra cosa", "--retry")
            self.assertEqual(json.loads(state.read_text())["tasks"]["VIK-4"]["state"], "ready")

    def test_protected_action_forces_human_and_tamper_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.cli(state, "add-task", "VIK-5", "--title", "T", "--component", "agents", "--type", "code", "--risk", "low", "--protected-action", "systemd")
            self.cli(state, "verify")
            data = json.loads(state.read_text())
            data["tasks"]["VIK-5"]["audit"][0]["actor"] = "otro"
            state.write_text(json.dumps(data))
            self.cli(state, "verify", ok=False)


if __name__ == "__main__":
    unittest.main()
