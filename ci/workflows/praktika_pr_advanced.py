"""
PR — full-featured workflow demo.

Covers all features: S3 artifacts, docker, cache digests, parametrized jobs,
CI DB reporting, merge-ready status, GH summary comments, secrets.
"""
from praktika import Artifact, Docker, Job, Secret, Workflow
from ci.settings.settings import RunnerLabels
from praktika.settings import Settings


artifact = Artifact.Config(name="greet", type=Artifact.Type.S3, path="./artifact.txt")

workflow = Workflow.Config(
    name="PR",
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    runs_on_label_prefix="pr-",
    ai_assist=Workflow.AIAssist.Config(
        enabled=True,
        provider="bedrock-anthropic",
        model="global.anthropic.claude-sonnet-5",
        # Consolidated drive mode: on a failure, hold dispatch of not-yet-started
        # jobs, let the in-flight wave drain, then consult once over the whole
        # failure set — so correlated failures (one root cause surfacing in
        # several jobs) are fixed in a single patch.
        drive_mode=Workflow.AIAssist.DriveMode.CONSOLIDATED,
        # Code Review is advisory (allow_failure): don't consult the advisor the
        # moment it fails. Its blocking findings are still weighed in the
        # consolidated consult (the whole failure set is shown there, excluded
        # jobs included), with the ai_review digest in context.
        # exclude_jobs=["Code Review"],
        # Optionally restrict the advisor to specific jobs (exact names or globs);
        # a failure only triggers it when the name matches. None = no restriction.
        # allow_jobs=["*Test*", "Build (*)"],
        # Project-specific guidance steering when the advisor should act, keyed
        # by job-name glob. "*" guidance is added on every consult; a glob entry
        # only when a matching job is in the failure set.
        extra_prompt={
            # Any test job: patch a clearly-outdated test, else leave it.
            "*Test*": (
                "When a test job fails because the test itself is outdated (e.g. "
                "it asserts a value the PR intentionally changed) and the fix is "
                "small and unambiguous, use cancel_and_patch to update the test. "
                "Only do this when the test is clearly the stale side — if the "
                "production code looks wrong, prefer continue/cancel_run and leave "
                "it for a human."
            ),
            # The Code Review job: weigh the reviewer's structured findings.
            "Code Review": (
                "When the Code Review job fails with a blocking verdict, its "
                "digest carries an `ai_review` object listing the reviewer's "
                "findings (path/line/body). Read each cited file with read_file "
                "to confirm the finding is a real defect, and when the fix is "
                "clear and unambiguous use cancel_and_patch to address it. If a "
                "finding is subjective, ambiguous, or the reviewer looks wrong, "
                "continue and leave it for the author."
            ),
        },
    ),
    jobs=[
        # Require a version bump whenever praktika / praktika-controller source
        # changes in the PR (script fetches the base version via the GitHub API;
        # the ephemeral-merge checkout has no base history locally).
        Job.Config(
            name="Version Bump Check",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/scripts/check_version_bump.py",
            enable_gh_auth=True,
            digest_config=Job.CacheDigestConfig(
                include_paths=[
                    "./ci/scripts/check_version_bump.py",
                    "./praktika",
                    "./pyproject.toml",
                    "./bootstrap/src",
                    "./bootstrap/pyproject.toml",
                    "./bootstrap/setup.py",
                ],
            ),
        ),
        Job.Config(
            name="Praktika Pytests",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/scripts/run_ci_pytests.py",
            digest_config=Job.CacheDigestConfig(
                include_paths=[
                    "./ci/scripts/run_ci_pytests.py",
                    "./ci/tests",
                    "./praktika",
                    "./pyproject.toml",
                ],
            ),
        ),
        # Assert the controller-resolved run metadata (base_git_history,
        # commit_authors, …) is present and correct inside a job, with the S3
        # repo snapshot + ephemeral merge enabled project-wide.
        Job.Config(
            name="Check CI Runtime",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/scripts/check_ci_runtime.py",
            digest_config=Job.CacheDigestConfig(
                include_paths=[
                    "./ci/scripts/check_ci_runtime.py",
                    "./praktika",
                ],
            ),
        ),
        # Ruff style check (ruff is baked into the runner image venv, see
        # ci/infrastructure/projects.py::_runtime_prebuilt_venvs). Wrapped so the
        # job emits a Result with ruff's findings in a `ruff` sub-result's info.
        Job.Config(
            name="Style Check",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/scripts/run_ruff.py",
            digest_config=Job.CacheDigestConfig(
                include_paths=[
                    "./praktika",
                    "./ci",
                    "./pyproject.toml",
                ],
            ),
        ),
        # S3 artifact with cache digest
        Job.Config(
            name="Build",
            runs_on=[RunnerLabels.SMALL_ARM],
            command='echo "Hello from praktika" > ./artifact.txt && python3 ./ci/tests/example_2/some_job_script.py',
            provides=[artifact.name],
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
        ),
        # Consumes artifact, also cached
        Job.Config(
            name="Test",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/jobs/consume_artifact.py",
            requires=[artifact.name],
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/jobs/consume_artifact.py"],
            ),
        ),
        # Docker job
        Job.Config(
            name="Docker Job",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
            run_in_docker="clickhouse/praktika-test",
        ),
        # Parametrized with digests
        *Job.Config(
            name="Parametrized",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            requires=[artifact.name],
        ).parametrize(
            Job.ParamSet(parameter={"key_1": [1, 2, "ABC"], "key_2": None}),
            Job.ParamSet(parameter={"key_1": [2, 3]}),
        ),
        # Native AI code review: consults an OpenAI model on Bedrock for a
        # structured result, then posts the summary / inline findings and
        # resolves its own threads from trusted job code (see praktika.ai_review).
        # allow_failure so a review hiccup never blocks merge; enable_gh_auth so
        # the job can post comments and manage review threads.
        Job.Config(
            name="Code Review",
            runs_on=[RunnerLabels.SMALL_ARM_BEDROCK],
            command=(
                "python3 -m praktika review --provider bedrock-openai "
                "--model global.openai.gpt-5.6-sol --reasoning-effort high "
                "--prompt ./ci/prompts/code_review.md --fail-for-draft-pr "
                "--fail-on-blocking"
            ),
            allow_failure=True,
            enable_gh_auth=True,
        ),
    ],
    artifacts=[artifact],
    dockers=[
        Docker.Config(
            name="clickhouse/praktika",
            path="./dockers/praktika",
            platforms=[Docker.Platforms.AMD, Docker.Platforms.ARM],
            depends_on=[],
        ),
        Docker.Config(
            name="clickhouse/praktika-test",
            path="./dockers/praktika-test",
            platforms=[Docker.Platforms.AMD, Docker.Platforms.ARM],
            depends_on=["clickhouse/praktika"],
        ),
    ],
    secrets=[
        Secret.Config(name=Settings.SECRET_CI_DB_CONNECTION, type=Secret.Type.AWS_SSM_PARAMETER),
        Secret.Config(name=Settings.SECRET_DOCKER_REGISTRY, type=Secret.Type.AWS_SSM_PARAMETER),
    ],
    enable_report=True,
    enable_cache=True,
    enable_merge_ready_status=True,
    enable_cidb=True,
    enable_gh_summary_comment=True,
    enable_exit_code_result=True,
    # Repo snapshot + ephemeral merge-commit are enabled project-wide via
    # Settings.ENABLE_S3_REPO_SNAPSHOT / ENABLE_PR_EPHEMERAL_MERGE_COMMIT
    # (ci/settings/settings.py), not per workflow. See praktika/docs/repo-git-management.md.
)

WORKFLOWS = [workflow]
