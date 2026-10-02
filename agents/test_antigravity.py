#!/usr/bin/env python3
"""Regresiones de la CLI headless de Antigravity, sin cuenta Google."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import providers
import team
import flow


FAKE_AGY = """#!/bin/sh
test \"$1\" = -p || exit 21
test \"$3\" = --output-format || exit 22
test \"$4\" = json || exit 23
test \"$5\" = --mode=accept-edits || exit 24
test \"$6\" = --dangerously-skip-permissions || exit 25
test -z \"${GEMINI_API_KEY-}\" || exit 26
printf '%s\\n' '{"conversation_id":"fake-1","status":"SUCCESS","response":"listo","usage":{"total_tokens":3}}'
printf '%s\\n' 'telemetria separada' >&2
"""

FAKE_AGY_STREAM = """#!/bin/sh
printf '%s\\n' '{"event":"init"}' '{"event":"result","result":{"response":"listo"}}'
printf '%s\\n' 'progreso' >&2
"""

FAKE_AGY_AUTH = """#!/bin/sh
if [ "$1" = -p ] && [ "$2" = /usage ] && [ "$3" = --output-format ] && [ "$4" = json ]; then
  printf '%s\\n' run >> "$AGY_AUTH_CALLS"
  printf '%s\\n' '{"status":"SUCCESS","response":"authenticated"}'
  exit 0
fi
exit 31
"""


class AntigravityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.binary = self.base / "agy"
        self.binary.write_text(FAKE_AGY)
        self.binary.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def test_provider_uses_agy_print_mode_and_keeps_stderr_separate(self):
        cfg = {"providers": {"anti": {"enabled": True, "binary": str(self.binary),
                                        "implement": ["-p", "{prompt}", "--output-format", "json", "--mode=accept-edits", "--dangerously-skip-permissions"],
                                        "review": ["-p", "{prompt}", "--output-format", "json", "--mode=plan"],
                                        "separate_stderr": True}}}
        path = self.base / "providers.json"
        path.write_text(json.dumps(cfg))
        previous = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "should-not-reach-agy"
        try:
            got = providers.Providers(path).run("anti", "implement", "tarea", self.base, 10)
        finally:
            if previous is None:
                del os.environ["GEMINI_API_KEY"]
            else:
                os.environ["GEMINI_API_KEY"] = previous
        self.assertEqual(got.rc, 0)
        self.assertEqual(json.loads(got.output)["response"], "listo")
        self.assertEqual(got.stderr.strip(), "telemetria separada")

    def test_provider_is_disabled_until_real_smoke(self):
        cfg = {"providers": {"anti": {"enabled": False, "binary": str(self.binary), "implement": ["-p", "{prompt}"]}}}
        path = self.base / "providers.json"
        path.write_text(json.dumps(cfg))
        self.assertEqual(providers.Providers(path).usable("anti", "implement"), (False, "deshabilitado"))

    def test_generic_driver_preserves_ndjson_stdout_verbatim(self):
        stream = self.base / "agy-stream"
        stream.write_text(FAKE_AGY_STREAM)
        stream.chmod(0o755)
        cfg = {"providers": {"anti": {"enabled": True, "binary": str(stream),
                                        "implement": ["-p", "{prompt}", "--output-format", "stream-json"],
                                        "separate_stderr": True}}}
        path = self.base / "providers.json"
        path.write_text(json.dumps(cfg))
        got = providers.Providers(path).run("anti", "implement", "tarea", self.base, 10)
        self.assertEqual([json.loads(line)["event"] for line in got.output.splitlines()], ["init", "result"])
        self.assertEqual(got.stderr.strip(), "progreso")

    def test_default_configuration_is_an_enabled_normal_cli_provider(self):
        cfg = json.loads((Path(providers.__file__).with_name("providers.json")).read_text())
        anti = cfg["providers"]["antigravity"]
        self.assertTrue(anti["enabled"])   # habilitado por decisión de Gustavo; sin sesión lo excluye el auth_check
        self.assertEqual(anti["binary"], "agy")
        self.assertNotIn("driver", anti)
        self.assertTrue(anti["separate_stderr"])
        self.assertIn("--output-format", anti["implement"])
        self.assertIn("--mode=plan", anti["review"])
        self.assertTrue(any(c.get("command", [None])[1:2] == ["-p"] for c in anti["auth_checks"]))
        self.assertTrue(any(c.get("not_equals") == "gemini" for c in anti["auth_checks"]))

    def test_auth_checks_require_account_session_and_reject_api_key_setting(self):
        auth = self.base / "agy-auth"
        auth.write_text(FAKE_AGY_AUTH); auth.chmod(0o755)
        settings = self.base / "settings.json"
        cfg = {"providers": {"anti": {"enabled": True, "binary": str(auth), "implement": ["-p", "{prompt}"],
                                       "auth_checks": [
                                           {"command": ["{binary}", "-p", "/usage", "--output-format", "json"], "path": "status", "equals": "SUCCESS", "fix": "sin sesión"},
                                           {"file": str(settings), "path": "modelProvider", "not_equals": "gemini", "allow_missing": True, "fix": "API key"}
                                       ]}}}
        path = self.base / "providers.json"; path.write_text(json.dumps(cfg))
        cfg["providers"]["anti"]["auth_checks"][0]["cache_s"] = 60
        path.write_text(json.dumps(cfg))
        calls = self.base / "auth-calls"
        previous = os.environ.get("AGY_AUTH_CALLS")
        os.environ["AGY_AUTH_CALLS"] = str(calls)
        p = providers.Providers(path)
        try:
            self.assertEqual(p.usable("anti", "implement"), (True, "ok"))
            self.assertEqual(p.usable("anti", "implement"), (True, "ok"))
        finally:
            if previous is None:
                del os.environ["AGY_AUTH_CALLS"]
            else:
                os.environ["AGY_AUTH_CALLS"] = previous
        self.assertEqual(calls.read_text().splitlines(), ["run"])
        settings.write_text('{"modelProvider":"gemini"}')
        self.assertEqual(p.usable("anti", "implement"), (False, "API key"))

    def test_agy_json_envelope_with_markdown_verdict_reaches_flow_parser(self):
        response = 'Revisé el cambio.\n```json\n{"verdict":"changes","summary":"falta una regresión","findings":["test faltante"]}\n```'
        output = json.dumps({"status": "SUCCESS", "response": response})
        expected = {
            "verdict": "changes", "summary": "falta una regresión", "findings": ["test faltante"]
        }
        self.assertEqual(flow.Flow.parse_verdict(output), expected)

    def test_agy_stream_json_with_markdown_verdict_reaches_flow_parser(self):
        response = 'Resultado final:\n```json\n{"verdict":"approve","summary":"correcto","findings":[]}\n```'
        output = '\n'.join([json.dumps({"event": "init"}), json.dumps({"event": "result", "result": {"response": response}})])
        self.assertEqual(flow.Flow.parse_verdict(output), {
            "verdict": "approve", "summary": "correcto", "findings": []
        })

    def test_explicit_provider_preference_can_select_secondary(self):
        cfg = {"version": 1, "levels": {}, "min_level_for_risk": {"low": 1}, "agents": {
            "Sashimi": {"title": "Analista", "roles": ["implementer"], "providers": ["gemini", "antigravity"],
                         "specialties": "code", "initial_trust": 1, "ceiling": 1, "persona": "x"}}}
        path = self.base / "team.json"
        path.write_text(json.dumps(cfg))

        class Available:
            def usable(self, name, mode):
                return True, "ok"

        picked = team.Team(path).pick("work", "code", "low", Available(), set(), {"Sashimi": 1}, {}, prefer="antigravity")
        self.assertEqual(picked, ("Sashimi", "antigravity"))

    def test_provider_preference_does_not_change_agent_ranking(self):
        cfg = {"version": 1, "levels": {}, "min_level_for_risk": {"low": 1}, "agents": {
            "Mejor": {"title": "Analista", "roles": ["implementer"], "providers": ["gemini"],
                       "specialties": "code", "initial_trust": 1, "ceiling": 1, "persona": "x"},
            "Alterno": {"title": "Analista", "roles": ["implementer"], "providers": ["antigravity", "gemini"],
                         "specialties": "code", "initial_trust": 1, "ceiling": 1, "persona": "x"}}}
        path = self.base / "team-ranked.json"; path.write_text(json.dumps(cfg))

        class Available:
            def usable(self, name, mode):
                return True, "ok"

        picked = team.Team(path).pick("work", "code", "low", Available(), set(), {"Mejor": 1, "Alterno": 1},
                                      {"Mejor": .9, "Alterno": .1}, prefer="antigravity")
        self.assertEqual(picked, ("Mejor", "gemini"))

    def test_jev_agent_specialty_preference_still_selects_named_agent(self):
        cfg = {"version": 1, "levels": {}, "min_level_for_risk": {"low": 1}, "agents": {
            "Sashimi": {"title": "Analista", "roles": ["implementer"], "providers": ["gemini"],
                         "specialties": "code", "initial_trust": 1, "ceiling": 1, "persona": "x"},
            "Nori": {"title": "Implementador", "roles": ["implementer"], "providers": ["gemini"],
                     "specialties": "code", "initial_trust": 1, "ceiling": 1, "persona": "x"}}}
        path = self.base / "team-specialty.json"; path.write_text(json.dumps(cfg))

        class Available:
            def usable(self, name, mode):
                return True, "ok"

        picked = team.Team(path).pick("work", "code", "low", Available(), set(), {"Sashimi": 1, "Nori": 1},
                                      {"Sashimi": .9, "Nori": .1}, prefer="Nori")
        self.assertEqual(picked, ("Nori", "gemini"))


if __name__ == "__main__":
    unittest.main()
