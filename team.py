"""El equipo de agentes: perfiles con nombre, roles, confianza y elección por tarea."""
from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
from paths import CONFIG
ROLE_FOR = {"code": "implementer", "data": "implementer", "test": "evaluator",
            "research": "researcher", "security": "evaluator", "release": "release_guard"}
EXECUTOR_ROLES = {"implementer", "evaluator", "researcher", "release_guard"}


class Team:
    def __init__(self, path: Path = CONFIG / "team.json"):
        self.cfg = json.loads(path.read_text(encoding="utf-8"))
        self.agents = self.cfg["agents"]

    def specialties(self) -> dict[str, str]:
        return {n: f"{a['title']}: {a['specialties']}" for n, a in self.agents.items() if EXECUTOR_ROLES & set(a["roles"])}

    def min_level(self, risk: str) -> int:
        return self.cfg["min_level_for_risk"][risk]

    def pick(self, kind: str, ttype: str, risk: str, providers, exclude_providers: set[str], levels: dict[str, int],
             scores: dict[str, float], prefer: str | None = None, mode: str = "implement", explore: float = 0.0):
        """kind: 'work' | 'review'. Devuelve (agente, proveedor) o None. `providers` es providers.Providers."""
        role = "reviewer" if kind == "review" else ROLE_FOR[ttype]
        need = self.min_level(risk) if kind == "work" else max(1, self.min_level(risk) - 1)
        ranked = []
        for name, a in self.agents.items():
            if role not in a["roles"] or levels.get(name, a["initial_trust"]) < need:
                continue
            choices = list(a["providers"])
            if prefer in choices:
                choices.remove(prefer)
                choices.insert(0, prefer)
            provider = next((p for p in choices if p not in exclude_providers and providers.usable(p, mode)[0]), None)
            if not provider:
                continue
            adv = providers.quota_advice(provider) if hasattr(providers, "quota_advice") else {"bonus": 0.0, "discourage_heavy": False}
            # el bonus de cuota (USAR +, FRENAR -) reordena; en riesgo alto un proveedor frenado pesa el doble
            bonus = adv["bonus"] * (2 if adv["discourage_heavy"] and risk == "high" else 1)
            fit = 1 if ttype in a["specialties"].lower() or ttype in a["title"].lower() else 0
            # `prefer` puede venir de `proveedor:` (prioriza esa alternativa)
            # o de JEV `specialty` (nombre del agente). Conservamos ambos
            # contratos: una especialidad concreta sigue priorizando a la
            # persona, sin premiar por accidente a quien sólo tenga ese CLI.
            ranked.append((name == prefer, scores.get(name, 0.5) + bonus + (random.random() * explore), fit, name, provider))
        ranked.sort(reverse=True)
        return (ranked[0][3], ranked[0][4]) if ranked else None

    def why_none(self, kind: str, ttype: str, risk: str, levels: dict[str, int]) -> str:
        role = "reviewer" if kind == "review" else ROLE_FOR[ttype]
        need = self.min_level(risk) if kind == "work" else max(1, self.min_level(risk) - 1)
        cands = [n for n, a in self.agents.items() if role in a["roles"]]
        ok = [n for n in cands if levels.get(n, self.agents[n]["initial_trust"]) >= need]
        if not cands:
            return f"el equipo no tiene ningún agente con rol {role}"
        if not ok:
            return f"ningún agente con rol {role} tiene confianza suficiente para riesgo {risk}"
        return f"los agentes con rol {role} ({', '.join(ok)}) no tienen un proveedor disponible/independiente ahora"
