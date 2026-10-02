"""JEV (Typesafe JEV-1.13 vía OpenRouter): clasificador rápido, consistente y barato para el flujo.

No es un agente que trabaja: es el "sensor" del flujo. Recibe un `state` (JSON) y `questions`
tipadas (`choice` con criterios, `noul` = probabilidad sí/no) y devuelve probabilidades y confianza
en ~0.6 s por ~USD 0.00003. Se usa donde hace falta clasificar y no razonar:

  triage      ticket nuevo -> tipo, riesgo, ¿toca systemd/cron/hardware/secretos?, ¿ambiguo?, especialidad
  screen      diff terminado -> ¿paths o comandos peligrosos?, ¿debilitó tests?, ¿fuera de alcance?
  verdict     texto libre del revisor -> approve | changes | escalate (cuando no trae JSON válido)
  categorize  hallazgos/fallas -> taxonomía fija (alimenta el ciclo de aprendizaje)

Reglas de uso (definidas en FLOW.md): JEV puede SUBIR el riesgo o pedir una persona, nunca bajarlo ni
aprobar por sí solo algo protegido; si no responde, el flujo cae a reglas deterministas; cada llamada
queda registrada (hash de la entrada, respuesta, costo, latencia) en `.runtime/jev/calls.jsonl`.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
KEY_FILE = Path.home() / ".config/sushi-inspector/openrouter.env"

TASK_TYPES = {"code": "Implement or change code", "data": "Dataset, labels or data pipeline work",
              "test": "Write or run tests", "research": "Investigate and report only, no code change",
              "security": "Security review or fix", "release": "Release, deploy or packaging"}
RISKS = {"low": "Reversible, isolated code, tests or docs",
         "medium": "Changes behaviour of running software but is reversible with git",
         "high": "Touches production services, hardware, credentials, or is hard to undo"}
FINDING_CATEGORIES = {"bug": "Logic error or wrong behaviour", "missing_tests": "Insufficient or weakened tests",
                      "scope_creep": "Changes beyond what was asked", "incomplete": "Requirement not fully done",
                      "misunderstood": "Misread the requirement", "security": "Secrets, unsafe commands, permissions",
                      "docs": "Missing or wrong documentation or records", "style": "Style, naming, structure",
                      "performance": "Slow or resource-heavy", "environment": "Tooling, paths or environment problem"}


def load_key() -> str | None:
    try:
        for line in KEY_FILE.read_text().splitlines():
            if "OPENROUTER_API_KEY=" in line and not line.lstrip().startswith("#"):
                return line.split("OPENROUTER_API_KEY=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return None


class Jev:
    def __init__(self, log_dir: Path, key: str | None = None, daily_cap: int = 400, transport=None):
        self.log = log_dir / "calls.jsonl"
        self.key = key if key is not None else load_key()
        self.cap, self.transport = daily_cap, transport

    def enabled(self) -> bool:
        return bool(self.key or self.transport) and self.calls_today() < self.cap

    def calls_today(self) -> int:
        try:
            return sum(1 for l in self.log.read_text().splitlines() if f'"day": "{date.today()}"' in l)
        except OSError:
            return 0

    def ask(self, purpose: str, state: dict, questions: dict) -> dict | None:
        """Devuelve {pregunta: respuesta} o None si JEV no está disponible (el llamador usa reglas)."""
        if not self.enabled():
            return None
        t0 = time.time()
        try:
            if self.transport:
                body = self.transport(state, questions)
            else:
                req = urllib.request.Request(URL, method="POST", data=json.dumps({"model": MODEL, "state": state, "questions": questions}).encode(),
                                             headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = json.load(r)
            answers = body["answers"]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            self._log(purpose, state, None, t0, 0, str(exc)[:150])
            return None
        self._log(purpose, state, answers, t0, body.get("usage", {}).get("cost", 0), "")
        return answers

    def _log(self, purpose, state, answers, t0, cost, err) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        entry = {"day": str(date.today()), "at": time.strftime("%FT%T"), "purpose": purpose, "ms": round((time.time() - t0) * 1000), "cost": cost,
                 "input": "sha256:" + hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16], "answers": answers, "error": err}
        with open(self.log, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---------- helpers de respuesta ----------
    @staticmethod
    def p(ans: dict, name: str) -> float:
        return float((ans.get(name) or {}).get("noul", 0.0))

    @staticmethod
    def choice(ans: dict, name: str) -> tuple[str | None, float, dict]:
        a = ans.get(name) or {}
        return a.get("choice"), float(a.get("confidence", 0.0)), a.get("probabilities", {})

    # ---------- usos concretos ----------
    def triage(self, title: str, description: str, component: str, specialties: dict[str, str]) -> dict | None:
        ans = self.ask("triage", {"title": title, "description": description[:3000], "component": component}, {
            "task_type": {"type": "choice", "instructions": "Classify the engineering ticket type.", "criteria": TASK_TYPES},
            "risk": {"type": "choice", "instructions": "Risk of an autonomous AI agent doing this on a production Raspberry Pi that runs a camera-based inspection system.", "criteria": RISKS},
            "protected": {"type": "noul", "instructions": "Does the ticket require changing or restarting systemd units, cron jobs, credentials/secrets, camera or other hardware, deployments, or deleting data?"},
            "ambiguous": {"type": "noul", "instructions": "Is the ticket too vague for an agent to act on without asking the human clarifying questions?"},
            "specialty": {"type": "choice", "instructions": "Which specialty fits best?", "criteria": specialties}})
        if not ans:
            return None
        ttype, tconf, _ = self.choice(ans, "task_type")
        _, rconf, rprobs = self.choice(ans, "risk")
        # riesgo conservador: si high tiene peso relevante, se sube aunque no sea la elección principal
        risk = "high" if rprobs.get("high", 0) >= 0.30 else ("medium" if rprobs.get("medium", 0) + rprobs.get("high", 0) >= 0.5 else "low")
        spec, _, _ = self.choice(ans, "specialty")
        return {"type": ttype, "type_conf": tconf, "risk": risk, "risk_conf": rconf, "protected": self.p(ans, "protected"),
                "ambiguous": self.p(ans, "ambiguous"), "specialty": spec}

    def screen(self, contract: dict, files: list[str], diff: str) -> dict | None:
        ans = self.ask("screen", {"task": contract, "files": files[:60], "diff_excerpt": diff[:6000]}, {
            "dangerous": {"type": "noul", "instructions": "Does the diff add destructive or dangerous operations (rm -rf on broad paths, disabling services, changing permissions/credentials, force-push, network exfiltration)?"},
            "weakens_tests": {"type": "noul", "instructions": "Does the diff weaken, delete or skip tests/assertions to make them pass?"},
            "off_task": {"type": "noul", "instructions": "Does the diff change things unrelated to the stated task?"}})
        return None if not ans else {k: self.p(ans, k) for k in ("dangerous", "weakens_tests", "off_task")}

    def verdict(self, review_text: str) -> tuple[str, float] | None:
        ans = self.ask("verdict", {"review": review_text[-4000:]}, {
            "verdict": {"type": "choice", "instructions": "What did this code reviewer conclude?",
                        "criteria": {"approve": "Reviewer accepts the work as is", "changes": "Reviewer asks for corrections", "escalate": "Reviewer says a human must decide or reports a serious risk"}}})
        if not ans:
            return None
        v, conf, _ = self.choice(ans, "verdict")
        return (v, conf) if v else None

    def categorize(self, texts: list[str]) -> list[str]:
        out = []
        for text in texts:
            ans = self.ask("categorize", {"finding": text[:600]}, {"category": {"type": "choice", "instructions": "Categorize this code review finding or failure reason.", "criteria": FINDING_CATEGORIES}})
            out.append(self.choice(ans, "category")[0] if ans else None)
        return out

    def validate_escalation(self, task: dict, trigger: str, context: dict) -> dict | None:
        """Evalúa si una escalación o pausa es coherente y legítima antes de molestar al humano."""
        state = {
            "task_title": task.get("title", ""),
            "task_type": task.get("type", "code"),
            "task_risk": task.get("risk", "medium"),
            "trigger": trigger,
            "files_touched": context.get("files", [])[:40],
            "diff_excerpt": context.get("diff", "")[:4000],
            "error_detail": context.get("error", "")[:1000],
        }
        questions = {
            "requires_human_decision": {
                "type": "noul",
                "instructions": "Is this a legitimate business/architecture/risk decision that ONLY a human can make (Yes), or a routine technical error / benign tooling change (No)?"
            },
            "is_safe_to_proceed": {
                "type": "noul",
                "instructions": "Is the change actually safe, non-destructive, and free of security risks despite touching a protected directory or trigger?"
            },
            "escalation_kind": {
                "type": "choice",
                "instructions": "What is the primary nature of this event?",
                "criteria": {
                    "technical_fault": "Git merge, environment, or tooling error to be handled technically",
                    "benign_change": "Safe tooling, status page, or documentation change",
                    "critical_decision": "High-impact production change requiring human consent",
                    "unclear_request": "Ambiguous ticket requirements needing user clarification"
                }
            }
        }
        ans = self.ask("validate_escalation", state, questions)
        if not ans:
            return None
        kind, kconf, _ = self.choice(ans, "escalation_kind")
        return {
            "requires_human": self.p(ans, "requires_human_decision"),
            "safe_to_proceed": self.p(ans, "is_safe_to_proceed"),
            "kind": kind,
            "kind_conf": kconf,
        }

