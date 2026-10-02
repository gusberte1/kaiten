import json
import tempfile
import unittest
from pathlib import Path

import quota
import providers as prov
import team as teammod

NOW = 1_790_000_000.0
H = 3600


def reading(group, window, used, rem_h, read_ago_h=0.0):
    return {"group": group, "window": window, "used": used, "reset": NOW + rem_h * H, "read": NOW - read_ago_h * H}


class Parsers(unittest.TestCase):
    def test_claude_usage_con_zona_de_dos_barras(self):
        txt = ("Current session: 18% used · resets Sep 28, 10:09pm (America/Argentina/Buenos_Aires)\n"
               "Current week (all models): 47% used · resets Sep 30, 7:59pm (America/Argentina/Buenos_Aires)\n")
        got = quota.parse_claude(txt, 1_790_000_000.0)
        self.assertEqual([(r["window"], r["used"]) for r in got], [("5h", 18.0), ("semanal", 47.0)])

    def test_claude_sin_datos_falla(self):
        with self.assertRaises(ValueError):
            quota.parse_claude("Not logged in", NOW)

    def test_agy_tsv(self):
        txt = "Gemini Models\tWeekly\t23%\t2026-10-01T10:00:00Z\nClaude and GPT models\tFive-hour\t100%\t2026-09-29T01:00:00Z\n"
        got = quota.parse_agy(txt, NOW)
        self.assertEqual([(r["group"], r["window"], r["used"]) for r in got],
                         [("agy:Gemini Models", "semanal", 77.0), ("agy:Claude and GPT models", "5h", 0.0)])

    def test_codex_rollout(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "2026" / "09" / "28" / "rollout-x.jsonl"
            f.parent.mkdir(parents=True)
            ev = {"timestamp": "2026-09-28T17:42:00Z", "payload": {"rate_limits": {
                "primary": {"window_minutes": 300, "used_percent": 5.0, "resets_at": 1790000000},
                "secondary": {"window_minutes": 10080, "used_percent": 40.0, "resets_at": 1790100000}}}}
            f.write_text("basura\n" + json.dumps(ev) + "\n")
            got = quota.read_codex(NOW, Path(d))
        self.assertEqual([(r["window"], r["used"]) for r in got], [("5h", 5.0), ("semanal", 40.0)])


class Analysis(unittest.TestCase):
    def test_estados_por_razon(self):
        se_agota = quota.analyze_window(reading("g", "semanal", 60, 84), NOW)     # mitad de semana, 60 % usado
        se_pierde = quota.analyze_window(reading("g", "semanal", 10, 84), NOW)
        sano = quota.analyze_window(reading("g", "semanal", 50, 84), NOW)
        self.assertEqual((se_agota["state"], se_pierde["state"], sano["state"]), ("red", "blue", "green"))

    def test_ventana_renovada_cuenta_como_libre(self):
        w = quota.analyze_window(reading("g", "5h", 90, -1), NOW)
        self.assertTrue(w["renewed"])
        self.assertEqual(w["avail"], 100.0)

    def test_lectura_vieja_es_sin_dato(self):
        a = quota.analyze({"readings": [reading("claude", "semanal", 50, 84, read_ago_h=3)]}, NOW)
        self.assertEqual(a["groups"][0]["verdict"], "SIN_DATO")

    def test_codex_tolera_lectura_de_horas(self):
        a = quota.analyze({"readings": [reading("codex", "semanal", 50, 84, read_ago_h=3)]}, NOW)
        self.assertNotEqual(a["groups"][0]["verdict"], "SIN_DATO")

    def test_agotada(self):
        a = quota.analyze({"readings": [reading("claude", "5h", 99, 2), reading("claude", "semanal", 20, 100)]}, NOW)
        g = a["groups"][0]
        self.assertEqual(g["verdict"], "AGOTADA")
        self.assertIsNotNone(g["until"])

    def test_semanal_manda_sobre_5h(self):
        a = quota.analyze({"readings": [reading("claude", "5h", 0, 3), reading("claude", "semanal", 80, 100)]}, NOW)
        self.assertEqual(a["groups"][0]["verdict"], "FRENAR")

    def test_semanal_con_margen_y_5h_al_limite_espera(self):
        a = quota.analyze({"readings": [reading("claude", "5h", 95 - 6, 4.5), reading("claude", "semanal", 5, 100)]}, NOW)
        self.assertIn(a["groups"][0]["verdict"], ("ESPERAR", "AGOTADA"))


class Index(unittest.TestCase):
    def idx(self, readings):
        return quota.analyze({"readings": readings}, NOW)["groups"][0]

    def test_sobra_cuota_es_usar_ya(self):
        g = self.idx([reading("claude", "semanal", 10, 84)])
        self.assertGreaterEqual(g["index"], 80)
        self.assertEqual(g["level"], "USAR YA")

    def test_se_agota_es_parar(self):
        g = self.idx([reading("claude", "semanal", 90, 84)])
        self.assertEqual(g["level"], "PARAR")

    def test_5h_acelerada_topa_en_neutro(self):
        g = self.idx([reading("claude", "semanal", 10, 84), reading("claude", "5h", 90, 3)])
        self.assertLessEqual(g["index"], 45)

    def test_agotada_e_indice_cero(self):
        g = self.idx([reading("claude", "5h", 99, 2), reading("claude", "semanal", 20, 100)])
        self.assertEqual((g["index"], g["level"]), (0, "AGOTADA"))


class Json(unittest.TestCase):
    def test_cuota_100_usada_da_json_valido(self):
        with tempfile.TemporaryDirectory() as d:
            qf = Path(d) / "q.json"
            qf.write_text(json.dumps({"readings": [reading("agy:Gemini Models", "semanal", 100, 15)]}))
            out = json.dumps(quota.api_payload(prov.DEFAULT_FILE, qf, NOW), allow_nan=False)
            self.assertIn("AGOTADA", out)


class Collect(unittest.TestCase):
    def test_fuente_caida_conserva_ultima_lectura(self):
        with tempfile.TemporaryDirectory() as d:
            qf, hf = Path(d) / "q.json", Path(d) / "h.jsonl"
            ok = lambda now: [reading("claude", "semanal", 30, 100)]
            quota.collect(NOW, {"claude": ok}, qf, hf)
            def boom(now):
                raise ValueError("sin red")
            raw = quota.collect(NOW + 900, {"claude": boom}, qf, hf)
            self.assertFalse(raw["sources"]["claude"]["ok"])
            self.assertEqual(len(raw["readings"]), 1)
            self.assertEqual(len(hf.read_text().splitlines()), 2)


class Gate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.qf = Path(self.d.name) / "quota.json"
        self.addCleanup(self.d.cleanup)

    def write(self, readings):
        import time
        now = time.time()
        rs = [{**r, "reset": now + (r["reset"] - NOW), "read": now} for r in readings]
        self.qf.write_text(json.dumps({"generated_at": "x", "sources": {}, "readings": rs}))

    def test_advise_sin_fuente(self):
        self.assertEqual(quota.advise("deepseek", {"deepseek": {}}, None)["verdict"], "SIN_DATO")

    def test_providers_bloquea_agotada_y_deja_pasar_sin_archivo(self):
        table = json.loads(prov.DEFAULT_FILE.read_text())
        p_no_file = prov.Providers(prov.DEFAULT_FILE, None, Path(self.d.name) / "no-existe.json")
        self.assertEqual(p_no_file.quota_advice("claude")["verdict"], "SIN_DATO")
        self.write([reading("claude", "5h", 99, 2), reading("claude", "semanal", 20, 100)])
        p = prov.Providers(prov.DEFAULT_FILE, None, self.qf)
        self.assertFalse(p.quota_advice("claude")["usable"])
        ok, why = p.usable("claude", "implement")
        if p.binary("claude"):
            self.assertFalse(ok)
            self.assertIn("cuota agotada", why)
        self.assertTrue(table["providers"]["claude"]["quota"])

    def test_pick_prefiere_proveedor_con_cuota_sobrante(self):
        self.write([reading("claude", "semanal", 10, 84), reading("codex", "semanal", 80, 84)])
        team = teammod.Team()
        p = prov.Providers(prov.DEFAULT_FILE, None, self.qf)
        p.usable = lambda name, mode: (name in ("claude", "codex"), "ok")
        levels = {n: 9 for n in team.agents}
        pick = team.pick("work", "code", "low", p, set(), levels, {n: 0.5 for n in team.agents})
        self.assertEqual(pick[1], "claude")


class Cooldown(unittest.TestCase):
    """El cooldown por cuota respeta el reinicio real: el del aviso del CLI y, si llega después, la lectura de quota.json."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.hf = Path(self.d.name) / "provider-health.json"
        self.qf = Path(self.d.name) / "quota.json"

    def write_quota(self, used_5h, read_offset_s):
        import time
        now = time.time()
        rs = [{"group": "claude", "window": "5h", "used": used_5h, "reset": now + 2 * H, "read": now + read_offset_s},
              {"group": "claude", "window": "semanal", "used": 30, "reset": now + 80 * H, "read": now + read_offset_s}]
        self.qf.write_text(json.dumps({"generated_at": "x", "sources": {}, "readings": rs}))

    def test_usa_la_hora_del_aviso_y_no_el_tope_fijo(self):
        import time
        p = prov.Providers(prov.DEFAULT_FILE, self.hf)
        now = time.time()
        reset = quota.parse_reset("8pm", "America/Argentina/Buenos_Aires", now)
        until = p.cooldown("claude", 5, "You've hit your session limit · resets 8pm (America/Argentina/Buenos_Aires)")
        self.assertAlmostEqual(until, min(reset + 120, now + 5 * H), delta=5)

    def test_sin_hora_legible_usa_el_tope(self):
        import time
        p = prov.Providers(prov.DEFAULT_FILE, self.hf)
        until = p.cooldown("claude", 5, "usage limit reached")
        self.assertAlmostEqual(until, time.time() + 5 * H, delta=5)

    def test_lectura_de_cuota_posterior_y_usable_levanta_el_cooldown(self):
        p = prov.Providers(prov.DEFAULT_FILE, self.hf, self.qf)
        p.cooldown("claude", 5, "usage limit reached")
        self.write_quota(used_5h=20, read_offset_s=+5)
        self.assertFalse(p._cooldown_active("claude"))
        self.assertNotIn("claude", json.loads(self.hf.read_text()))

    def test_lectura_anterior_al_bloqueo_no_lo_levanta(self):
        p = prov.Providers(prov.DEFAULT_FILE, self.hf, self.qf)
        self.write_quota(used_5h=20, read_offset_s=-600)
        p.cooldown("claude", 5, "usage limit reached")
        self.assertTrue(p._cooldown_active("claude"))

    def test_lectura_posterior_agotada_no_lo_levanta(self):
        p = prov.Providers(prov.DEFAULT_FILE, self.hf, self.qf)
        p.cooldown("claude", 5, "usage limit reached")
        self.write_quota(used_5h=99, read_offset_s=+5)
        self.assertTrue(p._cooldown_active("claude"))

    def test_ventana_renovada_despues_del_bloqueo_lo_levanta_aunque_la_lectura_sea_vieja(self):
        import time
        now = time.time()
        rs = [{"group": "codex", "window": "5h", "used": 100, "reset": now - 60, "read": now - 3 * H},
              {"group": "codex", "window": "semanal", "used": 60, "reset": now + 80 * H, "read": now - 3 * H}]
        self.qf.write_text(json.dumps({"generated_at": "x", "sources": {}, "readings": rs}))
        self.hf.write_text(json.dumps({"codex": {"until": now + 2 * H, "since": now - 2 * H}}))
        p = prov.Providers(prov.DEFAULT_FILE, self.hf, self.qf)
        self.assertFalse(p._cooldown_active("codex"))

    def test_formato_viejo_del_archivo_sigue_funcionando(self):
        import time
        self.hf.write_text(json.dumps({"deepseek": time.time() + H}))
        p = prov.Providers(prov.DEFAULT_FILE, self.hf, self.qf)
        self.assertTrue(p._cooldown_active("deepseek"))   # sin grupos de cuota: vale hasta el fin


if __name__ == "__main__":
    unittest.main()
