"""Make the Lambda source folder importable in tests exactly as Lambda imports it."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dep"))
