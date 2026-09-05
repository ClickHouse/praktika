import dataclasses

from praktika.runtime import RunConfig


def test_run_config_from_dict_deserializes_cache_jobs_separately():
    config = RunConfig.from_dict(
        {
            "name": "CI",
            "digest_jobs": {},
            "digest_dockers": {},
            "cache_success": ["Job"],
            "cache_success_base64": [],
            "cache_artifacts": {
                "artifact": {
                    "type": "success",
                    "sha": "artifact-sha",
                    "pr_number": 1,
                    "branch": "branch",
                    "workflow": "CI",
                }
            },
            "cache_jobs": {
                "Job": {
                    "type": "success",
                    "sha": "job-sha",
                    "pr_number": 2,
                    "branch": "branch",
                    "workflow": "CI",
                }
            },
            "filtered_jobs": {},
            "sha": "head-sha",
            "submodule_cache_hash": "",
            "custom_data": {},
        }
    )

    assert set(config.cache_artifacts) == {"artifact"}
    assert set(config.cache_jobs) == {"Job"}
    assert config.cache_artifacts["artifact"].sha == "artifact-sha"
    assert config.cache_jobs["Job"].sha == "job-sha"


def _minimal_config_dict(**overrides):
    obj = {
        "name": "CI",
        "digest_jobs": {},
        "digest_dockers": {},
        "cache_success": [],
        "cache_success_base64": [],
        "cache_artifacts": {},
        "cache_jobs": {},
        "filtered_jobs": {},
        "sha": "head-sha",
        "submodule_cache_hash": "",
        "custom_data": {},
    }
    obj.update(overrides)
    return obj


def test_run_config_merge_fields_default_when_absent():
    # Back-compat: a config serialized before merge-commit support (no merge_*
    # keys) must still deserialize, with the new fields defaulting to empty.
    config = RunConfig.from_dict(_minimal_config_dict())
    assert config.base_sha == ""
    assert config.merge_sha == ""
    assert config.merge_snapshot_key == ""


def test_run_config_merge_fields_round_trip():
    config = RunConfig.from_dict(
        _minimal_config_dict(
            base_sha="base-sha",
            merge_sha="merge-sha",
            merge_snapshot_key="bucket/ci_cache/merge-snapshots/v1/pull_request/pr-1/abc.tar.zst",
        )
    )
    assert config.base_sha == "base-sha"
    assert config.merge_sha == "merge-sha"
    assert (
        config.merge_snapshot_key
        == "bucket/ci_cache/merge-snapshots/v1/pull_request/pr-1/abc.tar.zst"
    )

    # Survives the dump/relay cycle: workflow_config is relayed to jobs as
    # dataclasses.asdict(...) (see native_jobs Config Workflow) and re-read via
    # from_dict on the other side.
    reloaded = RunConfig.from_dict(dataclasses.asdict(config))
    assert reloaded.merge_sha == "merge-sha"
    assert reloaded.base_sha == "base-sha"
    assert reloaded.merge_snapshot_key == config.merge_snapshot_key
