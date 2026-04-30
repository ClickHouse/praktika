"""Settings overrides that activate when ``PRAKTIKA_TEST_ACTIVE=1``.

Loaded automatically by ``praktika.settings._get_settings()`` because the
filename matches the ``*_overrides.py`` glob. Subprocesses spawned by
the test (the ``Runner._run`` job-command subprocess, and the
``python -m praktika.native_jobs`` Config Workflow subprocess) inherit
the env var and freshly import praktika.settings, so the overrides land
before any other code touches ``Settings.TEMP_DIR``.

Module-level names matching ``_USER_DEFINED_SETTINGS`` are picked up by
``_load_settings_module`` and copied onto the Settings instance.

The parent test process imports praktika.settings at unittest discovery
time (before ``setUp`` sets the env var), so this file's
``if`` branch is False there and the parent must mirror the overrides
manually in ``setUp``.
"""
import os


if os.environ.get("PRAKTIKA_TEST_ACTIVE") == "1":
    TEMP_DIR = "./ci/tmp/_test_runner"
    OUTPUT_DIR = TEMP_DIR
    INPUT_DIR = TEMP_DIR
    SECRET_CI_DB_URL = "DUMMY_TEST_CI_DB_URL_NONEXISTENT"
    SECRET_CI_DB_USER = "DUMMY_TEST_CI_DB_USER_NONEXISTENT"
    SECRET_CI_DB_PASSWORD = "DUMMY_TEST_CI_DB_PASSWORD_NONEXISTENT"
