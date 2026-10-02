"""Raíz del proyecto que el motor gestiona: `KAITEN_PROJECT` o, por defecto, el padre de la carpeta del motor."""
import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("KAITEN_PROJECT") or ENGINE.parent).resolve()
RUNTIME = ROOT / ".runtime"
