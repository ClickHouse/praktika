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
