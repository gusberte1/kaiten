"""Raíz del proyecto que el motor gestiona: `KAITEN_PROJECT` o detección automática."""
import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parent


def _resolve_root() -> Path:
    env_root = os.environ.get("KAITEN_PROJECT")
    if env_root:
        return Path(env_root).resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "flow-project.json").exists() or (cwd / "kaiten.project.json").exists():
        return cwd
    if (ENGINE.parent / "flow-project.json").exists() or (ENGINE.parent / "kaiten.project.json").exists():
        return ENGINE.parent.resolve()
    if (ENGINE / "examples" / "flow-project.json").exists():
        return (ENGINE / "examples").resolve()
    return ENGINE.parent.resolve()


ROOT = _resolve_root()
RUNTIME = ROOT / ".runtime"
CONFIG = ROOT / "flow"
