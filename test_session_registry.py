import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from agents import session_registry
except ImportError:
    import session_registry


class SessionRegistryTest(unittest.TestCase):
    def test_start_stop_active_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            reg_file = Path(td) / "active_sessions.json"
            s = session_registry.start_session(
                topic="Prueba OBB",
                component="tickets/2026.08.25_obb_bust_v2",
                provider="claude",
                ticket_id="VIK-3",
                registry_file=reg_file,
            )
            self.assertEqual(s["topic"], "Prueba OBB")
            self.assertEqual(s["status"], "active")
            
            active = session_registry.get_active_sessions(registry_file=reg_file)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["id"], s["id"])
            
            stopped = session_registry.stop_session(s["id"], outcome="done", notes="ok", registry_file=reg_file)
            self.assertIsNotNone(stopped)
            self.assertEqual(stopped["status"], "done")
            
            active_after = session_registry.get_active_sessions(registry_file=reg_file)
            self.assertEqual(len(active_after), 0)

    def test_tokenization_normalized(self):
        toks = session_registry.tokenize("Analizar diferencia entre clips sitió firebase y 8094")
        self.assertIn("clips", toks)
        self.assertIn("sitio", toks)
        self.assertIn("firebase", toks)
        self.assertIn("8094", toks)
        self.assertNotIn("entre", toks)
        self.assertNotIn("y", toks)

    def test_find_related_active_and_recent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            reg_file = td_path / ".runtime" / "active_sessions.json"
            
            # Crear una sesión activa
            session_registry.start_session(
                topic="Revisión de clips 8094",
                component="tickets/2026.09.20_registro_eventos",
                provider="antigravity",
                registry_file=reg_file,
            )
            
            # Crear una sesión histórica falsa
            ses_dir = td_path / "tickets" / "2026.09.20_registro_eventos" / "docs" / "sessions"
            ses_dir.mkdir(parents=True, exist_ok=True)
            doc_file = ses_dir / "2026-09-25-diagnostico-clips.md"
            doc_file.write_text("# 2026-09-25 — Diagnóstico clips 8094\n\n## Contexto\nDiscrepancia de clips firebase y 8094.\n", encoding="utf-8")
            
            res = session_registry.find_related(
                "clips 8094",
                repo_root=td_path,
                registry_file=reg_file,
            )
            self.assertEqual(len(res["active"]), 1)
            self.assertEqual(res["active"][0]["provider"], "antigravity")
            self.assertTrue(len(res["recent"]) >= 1)
            self.assertIn("2026-09-25-diagnostico-clips.md", res["recent"][0]["path"])

    def test_ticket_tagging_and_matching(self):
        # 1. Extracción de tickets
        self.assertEqual(session_registry.extract_ticket_id("[VIK-11] Sincronización"), "VIK-11")
        self.assertEqual(session_registry.extract_ticket_id("Tarea VIK-3 en curso"), "VIK-3")
        self.assertEqual(session_registry.extract_ticket_id("Sin ticket"), "")

        # 2. Formato de títulos con tags
        self.assertEqual(
            session_registry.format_session_title("Sincronización de eventos", ticket_id="VIK-11", status="Active"),
            "[VIK-11][Active] Sincronización de eventos"
        )
        self.assertEqual(
            session_registry.format_session_title("[VIK-11] Sincronización", status="Done"),
            "[VIK-11][Done] Sincronización"
        )
        self.assertEqual(
            session_registry.format_session_title("[VIK-11][WIP] Sincronización", status="Done"),
            "[VIK-11][Done] Sincronización"
        )

        # 3. Registro y cierre con tags
        with tempfile.TemporaryDirectory() as td:
            reg_file = Path(td) / "active_sessions.json"
            s = session_registry.start_session(
                topic="[VIK-11] Sincronización",
                component="tickets/2026.09.20_registro_eventos",
                provider="claude",
                registry_file=reg_file,
            )
            self.assertEqual(s["ticket_id"], "VIK-11")
            self.assertEqual(s["tagged_title"], "[VIK-11][Active] Sincronización")

            # Match exacto por ID de ticket
            res = session_registry.find_related("VIK-11", registry_file=reg_file)
            self.assertEqual(len(res["active"]), 1)
            self.assertTrue(res["active"][0]["is_ticket_match"])

            # Cierre actualiza tagged_title a [Done]
            stopped = session_registry.stop_session(s["id"], outcome="done", registry_file=reg_file)
            self.assertEqual(stopped["tagged_title"], "[VIK-11][Done] Sincronización")


if __name__ == "__main__":
    unittest.main()
