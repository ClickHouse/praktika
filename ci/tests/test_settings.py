from praktika.settings import _Settings, _load_settings_module


def test_liveness_timeout_defaults():
    settings = _Settings()

    assert settings.HEARTBEAT_INTERVAL_S == 30
    assert settings.RUNNER_PICKUP_TIMEOUT_S == 3600
    assert settings.HEARTBEAT_TIMEOUT_S == 300


def test_liveness_timeouts_are_user_overridable(tmp_path):
    settings_file = tmp_path / "settings.py"
    settings_file.write_text(
        "\n".join(
            [
                "HEARTBEAT_INTERVAL_S = 11",
                "RUNNER_PICKUP_TIMEOUT_S = 22",
                "HEARTBEAT_TIMEOUT_S = 33",
            ]
        ),
        encoding="utf-8",
    )
    settings = _Settings()

    _load_settings_module(settings_file, settings)

    assert settings.HEARTBEAT_INTERVAL_S == 11
    assert settings.RUNNER_PICKUP_TIMEOUT_S == 22
    assert settings.HEARTBEAT_TIMEOUT_S == 33
