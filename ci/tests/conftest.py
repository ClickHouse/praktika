from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SRC = REPO_ROOT / "bootstrap" / "src"

if str(BOOTSTRAP_SRC) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
