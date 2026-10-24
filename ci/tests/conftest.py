from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SRC = REPO_ROOT / "bootstrap" / "src"

if str(BOOTSTRAP_SRC) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _default_verbose_job_checks():
    """Insulate unit tests from the project's deployment-time JOB_CHECKS_MODE.

    ``ci/settings/settings.py`` sets ``JOB_CHECKS_MODE = "minimal"`` for this
    repo's own CI, which is loaded into the global ``Settings``. The existing
    per-job check tests assert the verbose lifecycle (the historical default),
    so default every test to "verbose"; tests that exercise minimal mode
    override this via monkeypatch. Keeps behavior assertions independent of the
    repo's chosen CI configuration."""
    from praktika.settings import Settings

    saved = getattr(Settings, "JOB_CHECKS_MODE", "verbose")
    Settings.JOB_CHECKS_MODE = "verbose"
    yield
    Settings.JOB_CHECKS_MODE = saved
