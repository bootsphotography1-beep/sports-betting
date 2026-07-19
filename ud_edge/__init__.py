"""ud-edge-bot: No-vig +EV detector for Underdog Fantasy player props."""
__version__ = "0.1.0"

# Load .env file at import time (no third-party dependency required)
from pathlib import Path as _Path
import os as _os

def _load_dotenv(path: _Path | None = None) -> None:
    env_path = path or (_Path(__file__).resolve().parent.parent / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in _os.environ:
            _os.environ[key] = val

_load_dotenv()

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass