#!/usr/bin/env python3
"""Flujo completo con proveedores falsos en un repo git temporal (sin red, sin agentes reales)."""
import json, shutil
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import flow
import learn

FAKE = r'''#!/bin/bash
# proveedor falso: $1=modo $2=cwd ; comportamiento por archivos en $FAKE_DIR
mode="$1"; cd "$2" || exit 1; me="$(basename "$0")"
b() { cat "$FAKE_DIR/$1" 2>/dev/null || echo "$2"; }
if [ "$mode" = implement ]; then
  case "$(b impl_$me ok)" in
    ok) mkdir -p comp/__pycache__; echo "v=1" > comp/feature.py; echo x > comp/__pycache__/f.pyc; echo "hecho por $me" ;;
    protected) mkdir -p comp system-setup; echo x > comp/feature.py; echo x > system-setup/a.txt ;;
    secret) mkdir -p comp; echo "tk_abcdefghijklmnopqrstuvwxyz0123" > comp/feature.py ;;
    nochange) echo "nada" ;;
    escalar) echo "ESCALAR: necesito acceso al hardware. Corré:"; echo; echo '`bash /abs/ruta/script.sh`'; echo; echo "y respondé /reintentar" ;;
  esac
else
  n=$(cat "$FAKE_DIR/count_$me" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_DIR/count_$me"
  case "$(b rev_$me approve)" in
    approve) echo '```json'; echo '{"verdict":"approve","summary":"ok","findings":[]}'; echo '```' ;;
    changes_once) if [ "$n" = 0 ]; then echo '```json'; echo '{"verdict":"changes","summary":"falta algo","findings":["agregar X"]}'; echo '```'; else echo '```json'; echo '{"verdict":"approve","summary":"ahora sí","findings":[]}'; echo '```'; fi ;;
    tamper) echo hack > comp/hacked.txt; echo '```json'; echo '{"verdict":"approve","summary":"ok","findings":[]}'; echo '```' ;;
    garbage) echo "no json" ;;
  esac
fi
'''


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.fake = root / "repo", root / "fake"
        self.repo.mkdir(); self.fake.mkdir()
        os.environ["FAKE_DIR"] = str(self.fake)
        for name in ("alpha", "beta"):
            p = self.fake / name
            p.write_text(FAKE); p.chmod(0o755)
        provs = {"version": 1, "providers": {n: {"enabled": True, "binary": str(self.fake / n), "implement": ["implement", "{cwd}"], "review": ["review", "{cwd}"], "trailer": f"{n} <x@y>"} for n in ("alpha", "beta")}}
        (root / "providers.json").write_text(json.dumps(provs))
        team = {"version": 1, "levels": {}, "min_level_for_risk": {"low": 1, "medium": 2, "high": 3}, "promote_every": 2, "agents": {
            "Alfa": {"title": "Impl", "roles": ["implementer", "evaluator", "researcher"], "providers": ["alpha"], "specialties": "code", "initial_trust": 2, "ceiling": 3, "persona": "Sos Alfa."},
            "Beto": {"title": "Rev", "roles": ["reviewer"], "providers": ["beta"], "specialties": "review", "initial_trust": 2, "ceiling": 3, "persona": "Sos Beto."}}}
        (root / "team.json").write_text(json.dumps(team))
        policy = json.loads((flow.HERE / "orchestration_policy.json").read_text())
        policy["flow"].update(provider_order_implement=["alpha", "beta"], provider_order_review=["beta", "alpha"], audit_first_n_per_provider=1, audit_sample_rate=0)
        (root / "policy.json").write_text(json.dumps(policy))
        self.git("init", "-q", "-b", "main")
        shutil.copy(flow.ROOT / "flow-project.json", self.repo / "flow-project.json")
        (self.repo / "comp").mkdir()
        (self.repo / "comp/check.py").write_text("import sys,os; sys.exit(0 if os.path.exists('comp/feature.py') else 1)\n")
        self.git("add", "-A"); self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.sent = []
        self.flow = flow.Flow(self.repo, root / "rt", root / "providers.json", root / "policy.json", notify=self.sent.append,
                              team_file=root / "team.json", jev_key="")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True, check=True).stdout

    def add(self, tid, risk="low", ttype="code", **kw):
        self.flow.add_local(tid, "Tarea " + tid, "comp", ttype, risk, {"test": "python3 comp/check.py", "scope": [], **kw}, "hacer la feature")

    def state(self, tid):
        return self.flow.tasks()["tasks"][tid]

    def behave(self, **kv):
        for k, v in kv.items():
            (self.fake / k).write_text(v)

    def test_worktree_podado_se_reengancha_sin_perder_commits(self):
        """Podar el worktree de un ticket en curso (para liberar disco) no puede resetear su rama a dev."""
        self.add("T90")
        wt = self.flow.ensure_worktree("T90")
        (wt / "comp/feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "trabajo"], cwd=wt, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
        base = self.flow.tmeta("T90")["base"]
        self.git("worktree", "remove", "--force", str(wt))
        wt2 = self.flow.ensure_worktree("T90")
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt2, capture_output=True, text=True).stdout.strip(), head)
        self.assertTrue((wt2 / "comp/feature.py").exists())
        self.assertEqual(self.flow.tmeta("T90")["base"], base)   # el diff del ticket sigue midiéndose desde su base original

    def test_con_poco_disco_poda_worktrees_inactivos_y_conserva_la_rama(self):
        self.add("T91")
        wt = self.flow.ensure_worktree("T91")
        self.assertEqual(self.flow.prune_idle_worktrees(min_free_gb=0), [])          # con disco de sobra no toca nada
        self.assertEqual(self.flow.prune_idle_worktrees(min_free_gb=1e9), ["T91"])  # con poco disco poda el inactivo
        self.assertFalse(wt.exists())
        self.assertTrue(self.git("branch", "--list", "agent/T91").strip())

    def test_happy_path_cross_review_merge_and_audit(self):
        self.add("T1")
        self.flow.tick()
        t = self.state("T1")
        self.assertEqual(t["state"], "done", t)
        self.assertFalse((self.repo / "comp/feature.py").exists())         # main y el checkout de la persona quedan intactos
        self.assertIn("v=1", self.git("show", "dev:comp/feature.py"))
        self.assertNotIn("__pycache__", self.git("ls-tree", "-r", "--name-only", "dev"))   # el controlador no commitea basura       # el ticket se integró en dev
        self.assertIn("Reviewed-By: Beto/beta", self.git("log", "-1", "--format=%B", "dev"))
        self.assertIn("merge(agents): T1", self.flow.release(False))
        self.assertIn("publicado", self.flow.release(True))                # release: dev -> main sólo por decisión humana
        self.assertTrue((self.repo / "comp/feature.py").exists())
        runs = self.flow.tasks()["runs"].values()
        self.assertEqual({(r["role"], r["agent"], r["provider"]) for r in runs}, {("implementer", "Alfa", "alpha"), ("reviewer", "Beto", "beta")})
        self.assertTrue(any("Muestra para auditar" in s for s in self.sent))  # primera tarea del proveedor se muestrea
        self.assertEqual(self.flow.orch("verify").startswith("cadenas"), True)
        bundle = next((self.flow.runs_dir / "T1").iterdir())
        self.assertTrue((bundle / "prompt.md").exists() or (bundle / "meta.json").exists())

    def test_reviewer_asks_changes_then_approves(self):
        self.add("T2")
        self.behave(rev_beta="changes_once")
        self.flow.tick()
        self.assertEqual(self.state("T2")["state"], "done")
        self.assertEqual(self.state("T2")["rounds"], 1)

    def test_protected_path_needs_human_then_merges_on_approve(self):
        self.add("T3")
        self.flow.cfg["protected_paths"].append("system-setup/**")
        self.flow.cfg["always_allowed_paths"].append("system-setup/**")
        self.behave(impl_alpha="protected")
        self.flow.tick()
        self.assertEqual(self.state("T3")["state"], "human_review")
        self.assertTrue(any("necesita tu decisión" in s for s in self.sent))
        self.assertNotIn("feature.py", self.git("ls-tree", "-r", "--name-only", "dev"))   # nada se integró sin la persona
        self.flow.decide("T3", "approve", "ok", "Gustavo")
        self.flow.tick()
        self.assertEqual(self.state("T3")["state"], "done")
        self.assertIn("system-setup/a.txt", self.git("ls-tree", "-r", "--name-only", "dev"))

    def test_secret_in_diff_escalates_and_cannot_be_approved(self):
        self.add("T4")
        self.behave(impl_alpha="secret")
        self.flow.tick()
        self.assertEqual(self.state("T4")["state"], "human_review")
        with self.assertRaises(flow.FlowError):
            self.flow.decide("T4", "approve", "", "Gustavo")
        self.assertNotIn("tk_abcdefghijklmnop", (self.flow.runs_dir / "T4").rglob("diff.patch").__next__().read_text())

    def test_reviewer_that_modifies_checkout_is_discarded_and_escalated(self):
        self.add("T5")
        self.behave(rev_beta="tamper")
        self.flow.tick()
        self.assertEqual(self.state("T5")["state"], "human_review")
        self.assertIn("modificó", self.state("T5")["human_reason"])

    def test_agent_can_request_a_human(self):
        self.add("T6")
        self.behave(impl_alpha="escalar")
        self.flow.tick()
        self.assertIn("hardware", self.state("T6")["human_reason"])
        self.assertIn("bash /abs/ruta/script.sh", self.state("T6")["human_reason"])   # el comando de las líneas siguientes no se pierde

    def test_no_independent_reviewer_means_wait_never_self_review(self):
        self.add("T7")
        self.flow.provs.table["beta"]["enabled"] = False
        self.flow.tick()
        self.assertEqual(self.state("T7")["state"], "in_review")           # esperando: alpha no se revisa a sí mismo

    def test_failing_tests_loop_then_escalate(self):
        self.add("T8")
        self.behave(impl_alpha="nochange", impl_beta="nochange")
        self.flow.tick()
        self.assertEqual(self.state("T8")["state"], "human_review")
        self.assertEqual(self.state("T8")["failures"], 3)

    def test_dirty_user_checkout_never_blocks_integration(self):
        (self.repo / "comp").mkdir(exist_ok=True)
        (self.repo / "comp/feature.py").write_text("trabajo de la persona sin commitear\n")
        self.add("T10")
        self.flow.tick()
        self.assertEqual(self.state("T10")["state"], "done")
        self.assertEqual((self.repo / "comp/feature.py").read_text(), "trabajo de la persona sin commitear\n")

    def test_role_follows_task_type(self):
        self.add("T11", ttype="test")
        self.flow.tick()
        roles = {r["role"] for r in self.flow.tasks()["runs"].values()}
        self.assertEqual(roles, {"evaluator", "reviewer"})

    def test_trust_gates_risk_and_learning_loop(self):
        st = self.flow.tasks()
        self.assertEqual(learn.levels(st, self.flow.team)["Alfa"], 2)
        self.add("T12", risk="high")                                         # Alfa (nivel 2) no alcanza para high (3)
        self.flow.tick()
        self.assertEqual(self.state("T12")["state"], "ready")
        self.add("T13")
        self.behave(rev_beta="changes_once")
        self.flow.tick()
        self.assertEqual(self.state("T13")["state"], "done")
        text = learn.lessons(self.flow.tasks(), "comp", "code")
        self.assertIn("agregar X", text)                                     # lo que corrigió el revisor vuelve al próximo prompt
        self.add("T14")
        self.flow.tick()
        prompt = next((self.flow.runs_dir / "T14").rglob("prompt.md"))
        self.assertIn("agregar X", prompt.read_text())
        self.flow.orch("mark", "T14", "--event", "audit.reviewed", "--field", "audit_verdict", "--value", '"bad"', "--actor", "human")
        self.assertLess(learn.levels(self.flow.tasks(), self.flow.team)["Alfa"], 3)
        self.assertIn("Retrospectiva", learn.retro(self.flow.tasks(), self.flow.team))

    def test_jev_triage_only_raises_risk_and_screen_can_escalate(self):
        calls = []

        def fake(state, questions):
            calls.append(list(questions))
            if "task_type" in questions:
                return {"answers": {"task_type": {"choice": "code", "confidence": 1, "probabilities": {}},
                                    "risk": {"choice": "medium", "confidence": .3, "probabilities": {"low": 0, "medium": .6, "high": .4}},
                                    "protected": {"noul": 0.9}, "ambiguous": {"noul": 0.1}, "specialty": {"choice": "Alfa", "confidence": 1, "probabilities": {}}}}
            if "dangerous" in questions:
                return {"answers": {"dangerous": {"noul": 0.95}, "weakens_tests": {"noul": 0}, "off_task": {"noul": 0}}}
            return {"answers": {}}
        self.flow.jev.transport = fake
        tri = self.flow.jev.triage("t", "d", "comp", {"Alfa": "x"})
        self.assertEqual(tri["risk"], "high")                                # high con 0.40 >= 0.30 se sube (conservador)
        self.assertGreaterEqual(tri["protected"], 0.9)
        self.add("T15")
        self.flow.tick()
        self.assertEqual(self.state("T15")["state"], "human_review")
        self.assertIn("JEV", self.state("T15")["human_reason"])
        self.assertTrue(self.flow.jev.log.exists())

    def test_jev_rescues_review_without_json_only_with_high_confidence(self):
        self.flow.jev.transport = lambda st, q: {"answers": {"verdict": {"choice": "approve", "confidence": .95, "probabilities": {}}}} if "verdict" in q else {"answers": {}}
        self.add("T16")
        self.behave(rev_beta="garbage")
        self.flow.tick()
        self.assertEqual(self.state("T16")["state"], "done")
        self.flow.jev.transport = lambda st, q: {"answers": {"verdict": {"choice": "approve", "confidence": .5, "probabilities": {}}}} if "verdict" in q else {"answers": {}}
        self.add("T17")
        self.flow.tick()
        self.assertEqual(self.state("T17")["state"], "human_review")         # confianza baja -> falla la revisión 3 veces -> persona

    def test_jev_validate_escalation_filters_benign_guards(self):
        def fake(st, q):
            if "is_safe_to_proceed" in q:
                return {
                    "answers": {
                        "requires_human_decision": {"noul": 0.1},
                        "is_safe_to_proceed": {"noul": 0.95},
                        "escalation_kind": {"choice": "benign_change", "confidence": 0.9, "probabilities": {}}
                    }
                }
            return {"answers": {}}
        self.flow.jev.transport = fake
        res = self.flow.jev.validate_escalation({"title": "Test"}, "guard.protected_paths", {"files": ["system-setup/status-page/status.py"]})
        self.assertIsNotNone(res)
        self.assertGreaterEqual(res["safe_to_proceed"], 0.9)
        self.assertEqual(res["kind"], "benign_change")


    def test_agents_never_inherit_paid_api_keys(self):
        probe = self.fake / "envprobe"
        probe.write_text('#!/bin/bash\nenv | grep -c "API_KEY"\n'); probe.chmod(0o755)
        provs = {"version": 1, "providers": {"p": {"enabled": True, "binary": str(probe), "implement": ["x"]}}}
        (self.fake / "pp.json").write_text(json.dumps(provs))
        os.environ["GEMINI_API_KEY"] = os.environ["OPENAI_API_KEY"] = "secreto"
        try:
            res = flow.prov.Providers(self.fake / "pp.json").run("p", "implement", "x", self.fake, 10)
        finally:
            del os.environ["GEMINI_API_KEY"], os.environ["OPENAI_API_KEY"]
        self.assertEqual(res.output.strip(), "0")

    def test_kanban_columns_follow_flow_states(self):
        class FakeVik:
            humans = {"gustavo"}; bots = set()
            def __init__(self):
                self.cols = [{"id": 1, "title": "To-Do"}, {"id": 2, "title": "Doing"}, {"id": 3, "title": "Done"}]
                self.moves, self.said, self.closed = [], [], []
            def columns(self): return [c["title"] for c in self.cols]
            def setup_columns(self, wanted, rename):
                for c in self.cols:
                    c["title"] = rename.get(c["title"], c["title"])
                have = {c["title"] for c in self.cols}
                for t in wanted:
                    if t not in have: self.cols.append({"id": 10 + len(self.cols), "title": t})
            def move(self, vid, column): self.moves.append((vid, column))
            def comment(self, vid, text): self.said.append(text); return {"id": 100 + len(self.said)}
            def comments(self, vid): return []
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}" + (f"#comment-{cid}" if cid else "")
            def close(self, vid): self.closed.append(vid)
            def tasks(self): return []
        fake = FakeVik()
        self.flow.tracker = fake
        self.assertIn("Tu decisión", self.flow.setup_kanban())
        self.assertEqual([c["title"] for c in fake.cols][:3], ["Backlog", "Trabajando", "Hecho"])   # renombró las de fábrica
        self.assertEqual(len(fake.cols), 7)
        self.flow.add_local("T20", "K", "comp", "code", "low", {"test": "python3 comp/check.py", "scope": []}, "x", vik=77)
        self.flow.sync_boards()
        self.assertEqual(fake.moves[-1], (77, "En cola"))
        self.flow.tick()
        self.assertEqual(self.state("T20")["state"], "done")
        self.assertEqual([c for _, c in fake.moves], ["En cola", "Trabajando", "En revisión", "Trabajando", "Hecho"])
        self.flow.sync_boards()
        self.assertEqual(len(fake.moves), 5)                                # sin cambios de estado no vuelve a mover

    def test_propose_comments_once_even_if_updated_changes(self):
        class V:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.said = []
            def comment(self, vid, text): self.said.append(text); return {"id": len(self.said)}
        v = V()
        self.flow.tracker = v
        vt = {"id": 9, "title": "T", "description": "hacer algo", "updated": "1"}
        self.flow.propose(vt)
        self.flow.propose({**vt, "updated": "2"})   # nuestro comentario movió `updated`: no debe comentar de nuevo
        self.assertEqual(len(v.said), 1)
        self.flow.propose({**vt, "updated": "3", "description": "hacer otra cosa"})   # cambió el contenido: sí
        self.assertEqual(len(v.said), 2)

    def test_proposals_wait_in_tu_decision_column(self):
        class FakeVik:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.moves = []
            def columns(self): return list(flow.COLUMNS)
            def move(self, vid, column): self.moves.append((vid, column))
        fake = FakeVik()
        self.flow.tracker = fake
        m = self.flow.meta()
        m["proposals"] = {"2": {"contrato": {}}, "3": {"contrato": {}, "ignored": True}, "4": {"contrato": {}, "taken": True}}
        self.flow.save_meta(m)
        self.flow.sync_boards()
        self.assertEqual(sorted(fake.moves), [(2, "🙋 Tu decisión"), (3, "Descartadas")])   # la tomada la mueve su estado, no la propuesta
        self.flow.sync_boards()
        self.assertEqual(len(fake.moves), 2)

    def test_human_comments_notes_questions_and_commands(self):
        class V:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.said, self.thread = [], []
            def columns(self): return []
            def comments(self, vid): return self.thread
            def comment(self, vid, text): self.said.append(text); return {"id": 100 + len(self.said)}
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}" + (f"#comment-{cid}" if cid else "")
            def close(self, vid): pass
            def tasks(self): return []
        v = V()
        self.flow.tracker = v
        self.add("T30")
        self.flow.set_tmeta("T30", vik=30)
        self.behave(impl_alpha="nochange", impl_beta="nochange")
        self.flow.tick()                                                     # 3 fallos -> human_review
        self.assertEqual(self.state("T30")["state"], "human_review")
        aviso = next(x for x in self.sent if "T30" in x and "necesita tu decisión" in x)
        self.assertRegex(aviso, r"🔗 http://x/tasks/30#comment-\d+")          # el aviso de Telegram lleva el link al comentario
        # 1) nota simple: se anota y llega al prompt de la próxima ejecución
        v.thread = [{"id": 1, "text": "<p>Ojo: usá el archivo comp/feature.py</p>", "author": "Gustavo"}]
        self.flow.ingest_comments()
        self.assertTrue(any("Anotado" in s and "/reintentar" in s for s in v.said))
        self.assertIn("comp/feature.py", self.flow.tmeta("T30")["notes"][0]["text"])
        # 2) pregunta con @bot: la responde un agente en solo lectura
        v.thread.append({"id": 2, "text": "@bot-Orchestrator dame el comando absoluto", "author": "Gustavo"})
        self.flow.ingest_comments()
        self.flow.answer_questions()
        self.assertTrue(any("responde" in s for s in v.said))
        # 3) comentarios de otros (bots) no cuentan; /reintentar decide y las notas llegan al prompt
        v.thread.append({"id": 3, "text": "/reintentar probá de nuevo", "author": "bot-Orchestrator"})
        self.flow.ingest_comments()
        self.assertEqual(self.state("T30")["state"], "human_review")
        v.thread.append({"id": 4, "text": "> 🙋 Necesito una decisión tuya\n> Respondé en el ticket\n\n/Reintentar probá de nuevo", "author": "Gustavo"})   # respuesta con cita y mayúscula
        self.behave(impl_alpha="ok", impl_beta="ok")
        self.flow.ingest_comments()
        self.assertEqual(self.state("T30")["state"], "ready")
        self.flow.tick()
        self.assertIn("Comentarios de la persona", "".join(p.read_text() for p in (self.flow.runs_dir / "T30").rglob("prompt.md")))
        self.assertEqual(self.state("T30")["state"], "done")
        # 3b) /rechazar cancela también una tarea que NO está esperando decisión (p. ej. en revisión)
        self.add("T32")
        self.flow.set_tmeta("T32", vik=32)
        self.assertEqual(self.state("T32")["state"], "ready")
        v.thread = [{"id": 20, "text": "/rechazar duplicado", "author": "Gustavo"}]
        self.flow.ingest_comments()
        self.assertEqual(self.state("T32")["state"], "rejected")
        # 4) /reabrir revive una rechazada
        self.add("T31")
        self.flow.set_tmeta("T31", vik=31)
        self.flow.orch("escalate", "T31", "--reason", "x")
        self.flow.decide("T31", "reject", "no", "Gustavo")
        v.thread = [{"id": 9, "text": "/reabrir dale otra vez", "author": "Gustavo"}]
        self.flow.ingest_comments()
        self.assertEqual(self.state("T31")["state"], "ready")

    def test_vik_comment_is_brief_and_links_flow_documentation(self):
        class V:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.payload = None
            def comment(self, vid, text): self.payload = text; return {"id": 1}

        client = V()
        self.flow.tracker = client
        self.flow.add_local("T33", "K", "comp", "code", "low", {"test": "python3 comp/check.py", "scope": []}, "x", vik=33)
        self.flow.say("T33", "detalle " * 80)

        self.assertLess(len(client.payload.split()), flow.vikunja_adapter.MAX_COMMENT_WORDS + 1)
        self.assertIn(flow.vikunja_adapter.FLOW_DOC_URL, client.payload)

    def test_proposal_comment_is_brief_and_preserves_instructions_and_markdown(self):
        class V:
            humans = {"gustavo"}; bots = {"bot-x"}
            def __init__(self): self.said = []
            def tasks(self): return [{"id": 88, "title": "Mejorar detector", "description": "Necesito mejorar el detector en agents", "done": False,
                                      "assignees": ["bot-x"], "updated": "t1", "priority": 3}]
            def comments(self, vid): return []
            def comment(self, vid, text): self.said.append(text); return {"id": 1}
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}"
        v = V()
        self.flow.tracker = v
        self.flow.cfg["auto_intake"] = False
        self.flow.intake()
        self.assertEqual(len(v.said), 1)
        comment = v.said[0]
        # Breve (< 50 palabras)
        self.assertLess(len(comment.split()), flow.vikunja_adapter.MAX_COMMENT_WORDS + 1)
        # Instrucciones accionables preservadas
        self.assertIn("/tomar", comment)
        self.assertIn("/ignorar", comment)
        # Markdown y saltos de línea preservados
        self.assertIn("```\nagente: listo\n", comment)
        self.assertIn("\n\n", comment)
        # Enlace específico a guía de contratos
        self.assertIn("contrato-del-ticket-descripci", comment)

    def test_notify_humans_comment_is_brief_and_preserves_how_and_link(self):
        class V:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.said = []
            def comments(self, vid): return []
            def comment(self, vid, text): self.said.append(text); return {"id": 1}
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}"
        v = V()
        self.flow.tracker = v
        self.add("T34")
        self.flow.set_tmeta("T34", vik=34)
        # Causa humana deliberadamente larga (> 50 palabras)
        long_reason = "El agente reportó una discrepancia grave en la configuración de seguridad y encontró que múltiples dependencias no están sincronizadas con la versión de producción requerida por el sistema operativo de la Raspberry y además hay un conflicto con systemd."
        self.flow.orch("escalate", "T34", "--reason", long_reason)
        self.flow.notify_humans()
        self.assertEqual(len(v.said), 1)
        comment = v.said[0]
        # Breve (< 50 palabras)
        self.assertLess(len(comment.split()), flow.vikunja_adapter.MAX_COMMENT_WORDS + 1)
        # Las instrucciones de cómo responder NUNCA se pierden
        self.assertIn("/aprobar", comment)
        self.assertIn("/rechazar", comment)
        self.assertIn("/reintentar", comment)
        # Markdown preservado (header y separador)
        self.assertIn("🙋 **Necesito una decisión tuya**", comment)
        self.assertIn("---", comment)
        # Enlace a guía de decisiones
        self.assertIn("Guía de decisiones", comment)

    def test_review_comment_is_brief_and_preserves_findings_markdown(self):
        class V:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.said = []
            def comment(self, vid, text): self.said.append(text); return {"id": 1}
            def columns(self): return []
            def move(self, vid, bucket): pass
        v = V()
        self.flow.tracker = v
        self.flow.add_local("T35", "K", "comp", "code", "low", {"test": "python3 comp/check.py", "scope": []}, "x", vik=35)
        self.behave(rev_beta="changes_once")
        self.assertEqual(self.flow.implement(self.state("T35")), "done")
        self.assertEqual(self.flow.review(self.state("T35")), "done")
        self.assertEqual(len(v.said), 2)  # implementación y la rama real de revisión
        comment = v.said[-1]
        self.assertLess(len(comment.split()), flow.vikunja_adapter.MAX_COMMENT_WORDS + 1)
        self.assertIn("- agregar X", comment)
        self.assertNotIn("](.runtime/", comment)
        self.assertIn("Revisión", comment)

    def test_real_lifecycle_comments_are_brief_and_navigable(self):
        """Ejercita implementación, revisión, cierre, fallo y respuesta; no arma textos a mano."""
        class V:
            humans = {"gustavo"}; bots = set()
            def __init__(self): self.said = []
            def comment(self, vid, text): self.said.append(text); return {"id": len(self.said)}
            def columns(self): return []
            def move(self, vid, bucket): pass
            def close(self, vid): pass
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}"
            def tasks(self): return []
            def comments(self, vid): return []

        v = V()
        self.flow.tracker = v
        self.add("T36"); self.flow.set_tmeta("T36", vik=36)
        self.flow.tick()  # implement -> review -> finish_ok
        self.assertEqual(self.state("T36")["state"], "done")

        self.add("T37"); self.flow.set_tmeta("T37", vik=37)
        self.behave(impl_alpha="nochange", impl_beta="nochange")
        self.flow.tick()  # finish_fail repetido -> decisión humana
        self.assertEqual(self.state("T37")["state"], "human_review")

        self.add("T38"); self.flow.set_tmeta("T38", vik=38, questions=[{"id": 1, "text": "@bot ¿cuál es el estado?"}])
        self.flow.answer_questions()  # rama real answer_questions

        self.assertTrue(any("implementó" in c for c in v.said))
        self.assertTrue(any("revisó" in c for c in v.said))
        self.assertTrue(any("Completada" in c for c in v.said))
        self.assertTrue(any("Ejecución" in c for c in v.said))
        self.assertTrue(any("responde" in c for c in v.said))
        for comment in v.said:
            self.assertLess(len(comment.split()), flow.vikunja_adapter.MAX_COMMENT_WORDS + 1)
            self.assertLessEqual(len(comment), flow.vikunja_adapter.MAX_COMMENT_CHARS)
            self.assertIn("https://", comment)
            self.assertNotIn("](.runtime/", comment)

    def test_quota_notice_with_rc0_counts_as_blocked_not_as_work(self):
        fake = self.fake / "limit"
        fake.write_text('#!/bin/bash\necho "You\'ve hit your session limit · resets 12:40pm (America/Argentina/Buenos_Aires)"\n'); fake.chmod(0o755)
        cfg = json.loads((self.fake.parent / "providers.json").read_text())
        cfg["blocked_regex"] = json.loads((flow.HERE / "providers.json").read_text())["blocked_regex"]
        cfg["providers"]["alpha"]["binary"] = str(fake)
        (self.fake.parent / "providers.json").write_text(json.dumps(cfg))
        self.flow.provs = flow.prov.Providers(self.fake.parent / "providers.json", self.flow.rt / "provider-health.json")
        self.add("T40")
        self.flow.tick()
        self.assertNotEqual(self.state("T40")["state"], "in_review")        # el aviso de cuota no se cuenta como trabajo hecho
        self.assertFalse(self.flow.provs.usable("alpha", "implement")[0])   # alpha queda en cooldown

    def test_evidence_files_are_staged_in_worktree_and_not_committed(self):
        (self.flow.rt).mkdir(parents=True, exist_ok=True)
        log = self.repo / ".runtime" / "check.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("salida del script\n")
        self.flow.add_local("T50", "E", "comp", "code", "low", {"test": "python3 comp/check.py", "scope": []}, f"la evidencia está en {log}")
        self.flow.tick()
        prompts = "".join(p.read_text() for p in (self.flow.runs_dir / "T50").rglob("prompt.md"))
        self.assertIn("`./.evidencia/`", prompts); self.assertIn("`check.log`", prompts)
        self.assertNotIn(".evidencia", self.git("ls-tree", "-r", "--name-only", "dev"))
        self.assertEqual(self.state("T50")["state"], "done")

    def test_ticket_without_contract_is_never_ignored_silently_and_tomar_takes_it(self):
        class V:
            humans = {"gustavo"}; bots = {"bot-x"}
            def __init__(self): self.said, self.thread = [], []
            def tasks(self): return [{"id": 77, "title": "Sumar algo", "description": "Necesito cambiar algo en agents para el flujo", "done": False,
                                      "assignees": ["bot-x"], "updated": "t1", "priority": 5}]
            def comments(self, vid): return self.thread
            def comment(self, vid, text): self.said.append(text); return {"id": 200 + len(self.said)}
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}"
            def columns(self): return []
            def close(self, vid): pass
        v = V()
        self.flow.tracker = v
        self.flow.cfg["auto_intake"] = False
        self.flow.intake()
        self.assertEqual(len(v.said), 1)
        self.assertIn("no tiene contrato", v.said[0])
        self.assertIn("/tomar", v.said[0])
        self.assertIn("agents", v.said[0])                                   # componente inferido de la descripción
        self.flow.intake()
        self.assertEqual(len(v.said), 1)                                     # no repite el comentario si nada cambió
        self.assertNotIn("VIK-77", self.flow.tasks()["tasks"])
        self.assertIn("SIN CONTRATO", self.flow.board())                     # el tablero lo muestra: nunca queda invisible
        v.thread = [{"id": 1, "text": "/ignorar", "author": "Gustavo"}]
        self.flow.ingest_proposals()                                         # /ignorar: deja de aparecer y de insistir
        self.assertNotIn("SIN CONTRATO", self.flow.board())
        self.flow.intake()
        self.assertEqual(len(v.said), 2)
        m = self.flow.meta(); m["proposals"]["77"]["ignored"] = False; self.flow.save_meta(m)   # cambió de idea
        v.thread = [{"id": 2, "text": "> propuesta\n\n/Tomar riesgo: high", "author": "Gustavo"}]
        self.flow.ingest_proposals()
        task = self.flow.tasks()["tasks"]["VIK-77"]
        self.assertEqual((task["risk"], task["component"], task["state"]), ("high", "agents", "ready"))
        self.flow.intake()
        self.assertEqual(self.flow.tmeta("VIK-77")["priority"], 5)           # la prioridad de Vikunja se refleja
        self.assertIn("VIK-77", self.flow.board())

    def test_meta_writes_from_two_processes_do_not_clobber_each_other(self):
        daemon_view = self.flow.meta()                                       # el daemon lee...
        self.flow.set_tmeta("VIK-9", contract={"test": "x"}, vik=9)          # ...la CLI toma un ticket mientras tanto...
        daemon_view["notified"].append("aviso")                              # ...y el daemon guarda lo suyo
        daemon_view["seen"]["9"] = 5
        self.flow.save_meta(daemon_view)
        m = self.flow.meta()
        self.assertEqual(m["tasks"]["VIK-9"]["contract"], {"test": "x"})     # antes se perdía
        self.assertEqual((m["notified"], m["seen"]), (["aviso"], {"9": 5}))
        a, b = self.flow.meta(), self.flow.meta()
        a["notified"].append("A"); b["notified"].append("B")
        self.flow.save_meta(a); self.flow.save_meta(b)
        self.assertEqual(sorted(self.flow.meta()["notified"]), ["A", "B", "aviso"])

    def test_contract_parsing_and_hook_block(self):
        k = flow.parse_contract("<p>agente: listo</p>\n- **Tipo:** `code`\nriesgo: low\ncomponente: comp\ntest: python3 comp/check.py")
        self.assertEqual((k["agente"], k["tipo"], k["riesgo"], k["test"]), ("listo", "code", "low", "python3 comp/check.py"))
        g = flow.parse_contract("alcance: agents/**\n- **Alcance extra:** `datos/**`\ntest: python3 -m unittest discover -s agents -p test_*.py")
        self.assertEqual((g["alcance"], g["alcance-extra"], g["test"]), ("agents/**", "datos/**", "python3 -m unittest discover -s agents -p test_*.py"))
        real = flow.parse_contract("agente: listo tipo: research riesgo: medium componente: system-setup alcance: agents/\\** proveedor: Sashimi\n\n## Objetivo\n\nEvaluar algo.")
        self.assertEqual((real["agente"], real["tipo"], real["componente"], real["alcance"], real["proveedor"]), ("listo", "research", "system-setup", "agents/**", "Sashimi"))
        hooks = self.repo / "agents/hooks/pre_run.d"
        hooks.mkdir(parents=True)
        (hooks / "10-no.sh").write_text("#!/bin/bash\necho 'ventana de congelamiento'; exit 2\n")
        (hooks / "10-no.sh").chmod(0o755)
        self.add("T9")
        self.flow.tick()
        self.assertIn("congelamiento", self.state("T9")["human_reason"])

    def test_auto_intake_takes_viable_ticket_without_manual_tomar(self):
        class V:
            humans = {"gustavo"}; bots = {"bot-x"}
            def __init__(self): self.said, self.thread = [], []
            def tasks(self): return [{"id": 88, "title": "Analizar métricas de clips", "description": "Analizar diferencia en tickets/2026.09.20_registro_eventos", "done": False,
                                      "assignees": ["bot-x"], "updated": "t1", "priority": 3}]
            def comments(self, vid): return self.thread
            def comment(self, vid, text): self.said.append(text); return {"id": 300 + len(self.said)}
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}"
            def columns(self): return []
            def close(self, vid): pass
        v = V()
        self.flow.tracker = v
        self.flow.cfg["auto_intake"] = True
        self.flow.intake()
        self.assertIn("VIK-88", self.flow.tasks()["tasks"])
        t = self.flow.tasks()["tasks"]["VIK-88"]
        self.assertEqual(t["type"], "research")
        self.assertEqual(t["component"], "tickets/2026.09.20_registro_eventos")
        self.assertEqual(t["state"], "ready")
        self.assertTrue(any("Tomada automáticamente" in s for s in v.said))

    def test_active_session_guards_against_collision(self):
        import session_registry
        reg_file = self.flow.rt / "active_sessions.json"
        s = session_registry.start_session("Analizar clips y registro de eventos", component="tickets/2026.09.20_registro_eventos", provider="antigravity", registry_file=reg_file)
        class V:
            humans = {"gustavo"}; bots = {"bot-x"}
            def __init__(self): self.said, self.thread = [], []
            def tasks(self): return [{"id": 99, "title": "Analizar clips", "description": "clips en tickets/2026.09.20_registro_eventos", "done": False,
                                      "assignees": ["bot-x"], "updated": "t1", "priority": 1}]
            def comments(self, vid): return self.thread
            def comment(self, vid, text): self.said.append(text); return {"id": 400 + len(self.said)}
            def link(self, vid, cid=None): return f"http://x/tasks/{vid}"
            def columns(self): return []
            def close(self, vid): pass
        v = V()
        self.flow.tracker = v
        self.flow.cfg["auto_intake"] = True
        
        # Con la sesión activa, intake detecta el conflicto y NO toma el ticket
        orig_reg = session_registry.ACTIVE_SESSIONS_FILE
        try:
            session_registry.ACTIVE_SESSIONS_FILE = reg_file
            self.flow.intake()
            self.assertNotIn("VIK-99", self.flow.tasks()["tasks"])
            self.assertTrue(any("Sesión activa en curso detectada" in msg for msg in v.said))
            
            # Una vez cerrada la sesión, intake lo toma libremente
            session_registry.stop_session(s["id"], outcome="done", registry_file=reg_file)
            self.flow.intake()
            self.assertIn("VIK-99", self.flow.tasks()["tasks"])
        finally:
            session_registry.ACTIVE_SESSIONS_FILE = orig_reg

    def test_finalize_escalates_on_dev_merge_failure(self):
        class V:
            def __init__(self):
                self.said = []
                self.humans, self.bots = set(), set()

            def tasks(self):
                return []

            def comments(self, vid):
                return []

            def columns(self):
                return []

            def link(self, vid, comment_id=None):
                return f"https://vik.local/tasks/{vid}"

            def comment(self, vid, text):
                self.said.append(text)
                return {"id": len(self.said)}

        v = V()
        self.flow.tracker = v
        self.add("TMergeFail")
        self.flow.set_tmeta("TMergeFail", vik=34)
        self.behave(impl_alpha="ok", rev_beta="approve")
        orig_merge = self.flow.merge
        self.flow.merge = lambda *args, **kwargs: "dev no se pudo sincronizar con main. Archivos en conflicto: comp/feature.py. Salida de Git: CONFLICT"
        try:
            self.flow.tick()
            self.assertEqual(self.state("TMergeFail")["state"], "human_review")
            self.assertIn("falló la integración en dev", self.state("TMergeFail")["human_reason"])
            self.assertTrue(any("comp/feature.py" in comment for comment in v.said))
            self.assertTrue(any("git merge main" in comment for comment in v.said))
        finally:
            self.flow.merge = orig_merge

    def test_failed_main_sync_reports_conflicts_and_cleans_integration_worktree(self):
        wt = self.flow.ensure_integration()
        (wt / "comp/check.py").write_text("dev version\n")
        self.flow.git("add", "comp/check.py", cwd=wt)
        self.flow.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "dev change", cwd=wt)
        (self.repo / "comp/check.py").write_text("main version\n")
        self.git("add", "comp/check.py")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "main change")

        err = self.flow.merge({"id": "TConflict", "run_ids": []}, {}, [], {"runs": {}}, [])

        self.assertIn("comp/check.py", err)
        self.assertIn("dev no se pudo sincronizar con main", err)
        self.assertEqual(self.flow.git("status", "--porcelain", cwd=wt), "")
        self.assertEqual(self.flow.git("show", "dev:comp/check.py").strip(), "dev version")

    def test_reset_quota_clears_daily_count_and_notifications(self):
        self.flow.bump_daily()
        self.flow.bump_daily()
        self.assertGreaterEqual(self.flow.daily_count(), 2)
        m = self.flow.meta()
        m["notified"].append(f"dailylimit:{flow.date.today()}")
        self.flow.save_meta(m)
        prev = self.flow.reset_quota()
        self.assertGreaterEqual(prev, 2)
        self.assertEqual(self.flow.daily_count(), 0)
        self.assertNotIn(f"dailylimit:{flow.date.today()}", self.flow.meta().get("notified", []))

    def test_set_limit_updates_policy_and_runtime(self):
        orig_policy = self.flow.policy_file.read_text(encoding="utf-8")
        try:
            old = self.flow.set_limit(75)
            self.assertEqual(self.flow.cfg["daily_run_limit"], 75)
            pol = json.loads(self.flow.policy_file.read_text(encoding="utf-8"))
            self.assertEqual(pol["flow"]["daily_run_limit"], 75)
        finally:
            self.flow.policy_file.write_text(orig_policy, encoding="utf-8")
            self.flow.cfg["daily_run_limit"] = json.loads(orig_policy)["flow"]["daily_run_limit"]

    def test_set_limit_rejects_non_positive_values(self):
        with self.assertRaises(flow.FlowError):
            self.flow.set_limit(0)

    def test_daily_limit_warns_at_80_percent(self):
        orig_policy = self.flow.policy_file.read_text(encoding="utf-8")
        try:
            self.flow.set_limit(10)
            self.flow.cfg["daily_run_limit"] = 99  # simula un daemon que ya estaba en memoria
            m = self.flow.meta()
            m["daily"] = {str(flow.date.today()): 8}
            self.flow.save_meta(m)
            self.assertFalse(self.flow.daily_limit_hit())
            self.assertEqual(self.flow.cfg["daily_run_limit"], 10)
            self.assertIn(f"dailywarn80:{flow.date.today()}", self.flow.meta()["notified"])
        finally:
            self.flow.policy_file.write_text(orig_policy, encoding="utf-8")

    def test_prune_stale_worktrees(self):
        wt_dir = self.flow.wt_dir
        done_wt = wt_dir / "VIK-DONE-01"
        active_wt = wt_dir / "VIK-ACTIVE-01"
        integ_wt = wt_dir / "_integration"
        done_wt.mkdir(parents=True, exist_ok=True)
        active_wt.mkdir(parents=True, exist_ok=True)
        integ_wt.mkdir(parents=True, exist_ok=True)

        # Mock tasks in orchestrator state
        orig_tasks = self.flow.tasks
        self.flow.tasks = lambda: {
            "tasks": {
                "VIK-DONE-01": {"state": "done"},
                "VIK-ACTIVE-01": {"state": "executing"},
            }
        }
        try:
            pruned = self.flow.prune_stale_worktrees()
            self.assertIn("VIK-DONE-01", pruned)
            self.assertFalse(done_wt.exists())
            self.assertTrue(active_wt.exists())
            self.assertTrue(integ_wt.exists())
        finally:
            self.flow.tasks = orig_tasks
            if active_wt.exists():
                active_wt.rmdir()

    def test_normalize_test_cmd_converts_dotted_directory_paths(self):
        raw = "python3 -m unittest tickets/2026.09.20_registro_eventos/test_sync.py"
        norm = flow.Flow.normalize_test_cmd(raw)
        self.assertEqual(norm, "python3 -m unittest discover -s tickets/2026.09.20_registro_eventos -p test_sync.py")

        simple = "python3 -m unittest agents/test_flow.py"
        self.assertEqual(flow.Flow.normalize_test_cmd(simple), simple)


if __name__ == "__main__":
    unittest.main()
