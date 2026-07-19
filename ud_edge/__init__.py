"""ud-edge-bot: No-vig +EV detector for Underdog Fantasy player props."""
__version__ = "0.1.0"

# Load .env file at import time
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass