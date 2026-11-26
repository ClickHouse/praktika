import copy
import importlib.util
from pathlib import Path
from typing import List

from praktika import Workflow

from . import Job
from .info import Info
from .settings import Settings
from .utils import Utils
from .version import current_praktika_version


def _is_local_run():
    """Check if running locally. Returns False if can't determine (e.g., during workflow generation)."""
    try:
        return Info().is_local_run
    except Exception:
        # During workflow generation, Info can't be initialized - treat as CI mode
        return False


def _is_default_local_workflow(py_file: Path, workflow_name: str) -> bool:
    configured = Settings.DEFAULT_LOCAL_TEST_WORKFLOW
    if not configured:
        return False
    configured_values = {
        configured,
        Path(configured).name,
        Path(configured).stem,
    }
    workflow_values = {
        workflow_name,
        py_file.name,
        py_file.stem,
    }
    return bool(configured_values & workflow_values)


def _get_workflows(
    name=None,
    file=None,
    _for_validation_check=False,
    _file_names_out=None,
    _load_errors_out=None,
    default=False,
) -> List[Workflow.Config]:
    """
    Gets user's workflow configs
    """
    res = []

    if not Path(Settings.WORKFLOWS_DIRECTORY).is_dir():
        Utils.raise_with_error(
            f"Workflow directory does not exist [{Settings.WORKFLOWS_DIRECTORY}]. cd to the repo's root?"
        )
    directory = Path(Settings.WORKFLOWS_DIRECTORY)
    for py_file in directory.glob("*.py"):
        if not default:
            if Settings.ENABLED_WORKFLOWS:
                if not any(
                    py_file.name == Path(enabled_wf_file).name
                    for enabled_wf_file in Settings.ENABLED_WORKFLOWS
                ):
                    print(
                        f"NOTE: Workflow [{py_file.name}] is not enabled in Settings.ENABLED_WORKFLOWS - skip"
                    )
                    continue
            if Settings.DISABLED_WORKFLOWS:
                if any(
                    py_file.name == Path(disabled_wf_file).name
                    for disabled_wf_file in Settings.DISABLED_WORKFLOWS
                ):
                    print(
                        f"NOTE: Workflow [{py_file.name}] is disabled via Settings.DISABLED_WORKFLOWS - skip"
                    )
                    continue
            if file and str(file) not in str(py_file):
                continue
        module_name = py_file.name.removeprefix(".py")
        spec = importlib.util.spec_from_file_location(
            module_name, f"{Settings.WORKFLOWS_DIRECTORY}/{module_name}"
        )
        assert spec
        foo = importlib.util.module_from_spec(spec)
        assert spec.loader
        try:
            spec.loader.exec_module(foo)
        except Exception as e:
            # One workflow file that fails to import must not take down the whole
            # scan (and with it every other workflow). This notably happens on a
            # version-skewed pool: a workflow file references a newer praktika
            # feature (e.g. a new Workflow.Engine member) than the praktika the
            # orchestrator is running, so importing it raises AttributeError. Skip
            # the file with a clear warning so the remaining workflows still load
            # and match. Validation re-raises so `praktika validate` (run under the
            # checkout's own praktika) still catches genuinely broken files loudly.
            if _for_validation_check:
                raise
            print(
                f"WARNING: skipping workflow file [{py_file.name}] — failed to "
                f"import: {type(e).__name__}: {e}"
            )
            if isinstance(_load_errors_out, list):
                _load_errors_out.append((py_file.name, f"{type(e).__name__}: {e}"))
            continue
        try:
            matched_default_workflow = False
            for workflow in foo.WORKFLOWS:
                if default and not _is_default_local_workflow(py_file, workflow.name):
                    continue
                matched_default_workflow = True
                if name:
                    if name == workflow.name:
                        print(f"Read workflow [{name}] config from [{module_name}]")
                        res = [workflow]
                        break
                    else:
                        continue
                else:
                    res += [workflow]
                    print(
                        f"Read workflow configs from [{module_name}], workflow name [{workflow.name}]"
                    )
                if isinstance(_file_names_out, list):
                    _file_names_out.append(py_file.name.removeprefix(".py"))
            if default and not matched_default_workflow and not _is_local_run():
                print(f"Skip [{py_file.name}]")
        except Exception:
            pass
            # print(
            #     f"WARNING: Failed to add WORKFLOWS config from [{module_name}], exception [{e}]"
            # )
    if not res:
        if isinstance(_load_errors_out, list) and _load_errors_out:
            # Every workflow file failed to import (e.g. a pool on an older baked
            # praktika than the workflows use). Return empty instead of raising an
            # opaque "no workflow found" so the caller can surface the collected
            # load errors (and finalize the bootstrap check) rather than crash.
            return res
        Utils.raise_with_error(f"Failed to find [{name or file or 'any'}] workflow")

    if not _for_validation_check:
        for wf in res:
            # add native jobs
            _update_workflow_with_native_jobs(wf)
            # fill in artifact properties, e.g. _provided_by
            _update_workflow_artifacts(wf)
    return res


def _get_infra_projects():
    """
    Returns the infra configs imported as:
    Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH import PROJECTS
    """
    from pathlib import Path

    if not Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH:
        Utils.raise_with_error(
            "Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH is not set. "
            "Please define it in your ci/settings/*.py file."
        )

    config_path = Path(Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH)

    if not config_path.exists():
        Utils.raise_with_error(
            f"Infrastructure config file does not exist [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}]"
        )

    module_name = config_path.stem
    spec = importlib.util.spec_from_file_location(module_name, config_path)

    if not spec or not spec.loader:
        Utils.raise_with_error(
            f"Failed to load infrastructure config from [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}]: "
            f"path must point to a Python (.py) file, not a directory"
        )

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except TypeError as e:
        message = str(e)
        if "__init__()" in message and "unexpected keyword argument" in message:
            Utils.raise_with_error(
                "Infrastructure config cannot be loaded because there is a mismatch "
                "between the Praktika version and the infrastructure config.\n"
                f"Config file: {Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}\n"
                f"Running Praktika version: {current_praktika_version()}\n"
                f"Import error: {message}\n"
                "The config likely uses newer infrastructure fields that this "
                "Praktika runtime does not understand. Run the Praktika package "
                "version that matches this config, then retry."
            )
        raise

    try:
        projects = list(getattr(module, "PROJECTS"))
        if not projects:
            Utils.raise_with_error(
                f"PROJECTS in [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}] must contain at least one config"
            )
        seen_names = set()
        for project in projects:
            if not getattr(project, "name", ""):
                Utils.raise_with_error(
                    f"All PROJECTS in [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}] must have non-empty names"
                )
            if project.name in seen_names:
                Utils.raise_with_error(
                    f"Duplicate infrastructure project name [{project.name}] in [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}]"
                )
            seen_names.add(project.name)
            project._settings = Settings
        print(
            f"Loaded {len(projects)} infrastructure project(s) from [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}]"
        )
        return projects
    except AttributeError:
        Utils.raise_with_error(
            f"PROJECTS variable not found in [{Settings.CLOUD_INFRASTRUCTURE_CONFIG_PATH}]"
        )

    return None


def _get_infra_config(project_name=None, require_project: bool = False):
    projects = _get_infra_projects()
    if require_project and not project_name:
        names = ", ".join(sorted(project.name for project in projects))
        Utils.raise_with_error(
            f"Infrastructure destroy requires --project NAME. Available projects: {names}"
        )

    if len(projects) == 1:
        only = projects[0]
        if project_name and project_name != only.name:
            Utils.raise_with_error(
                f"Unknown infrastructure project [{project_name}]. Only configured project is [{only.name}]"
            )
        return only

    if not project_name:
        names = ", ".join(sorted(project.name for project in projects))
        Utils.raise_with_error(
            f"Multiple infrastructure projects configured [{names}]. Use --project NAME."
        )

    for project in projects:
        if project.name == project_name:
            return project

    Utils.raise_with_error(
        f"Unknown infrastructure project [{project_name}]. "
        f"Available projects: {', '.join(sorted(project.name for project in projects))}"
    )


def _get_artifact_to_providing_job_map(workflow):
    artifact_job = {}
    for job in workflow.jobs:
        for artifact_name in job.provides:
            artifact_job[artifact_name] = job.name
    return artifact_job


def _update_workflow_artifacts(workflow):
    artifact_job = _get_artifact_to_providing_job_map(workflow)
    for artifact in workflow.artifacts:
        if artifact.name in artifact_job:
            artifact._provided_by = artifact_job[artifact.name]


def _native_job_runs_on(workflow):
    return copy.deepcopy(workflow.native_job_runs_on or Settings.CI_CONFIG_RUNS_ON)


def _update_workflow_with_native_jobs(workflow):
    # Isolate shared Job.Config instances: the same objects are commonly referenced
    # by multiple workflows (one job_configs module), and the steps below mutate
    # job attributes in place (notably runs_on_label_prefix). Without this, one
    # workflow's mangling leaks into another.
    workflow.jobs = [copy.deepcopy(j) for j in workflow.jobs]

    if workflow.dockers and not workflow.disable_dockers_build:
        from .native_jobs import (
            _docker_build_amd_linux_job,
            _docker_build_arm_linux_job,
            _docker_build_manifest_job,
        )

        docker_job_names = []
        docker_digest_config = Job.CacheDigestConfig()
        for docker_config in workflow.dockers:
            docker_digest_config.include_paths.append(docker_config.path)

        if not Settings.ENABLE_MULTIPLATFORM_DOCKER_IN_ONE_JOB:
            aux_job = copy.deepcopy(_docker_build_amd_linux_job)
            if not _is_local_run():
                print(f"Enable praktika job [{aux_job.name}] for [{workflow.name}]")
            if workflow.enable_cache:
                if not _is_local_run():
                    print(f"Add automatic digest config for [{aux_job.name}] job")
                aux_job.digest_config = docker_digest_config
            workflow.jobs.insert(len(docker_job_names), aux_job)
            docker_job_names.append(aux_job.name)

            aux_job = copy.deepcopy(_docker_build_arm_linux_job)
            if not _is_local_run():
                print(f"Enable praktika job [{aux_job.name}] for [{workflow.name}]")
            if workflow.enable_cache:
                if not _is_local_run():
                    print(f"Add automatic digest config for [{aux_job.name}] job")
                aux_job.digest_config = docker_digest_config
            workflow.jobs.insert(len(docker_job_names), aux_job)
            docker_job_names.append(aux_job.name)

        if (
            workflow.enable_dockers_manifest_merge
            or Settings.ENABLE_MULTIPLATFORM_DOCKER_IN_ONE_JOB
        ):
            aux_job = copy.deepcopy(_docker_build_manifest_job)
            if not _is_local_run():
                print(f"Enable praktika job [{aux_job.name}] for [{workflow.name}]")
            if workflow.enable_cache:
                if not _is_local_run():
                    print(f"Add automatic digest config for [{aux_job.name}] job")
                aux_job.digest_config = docker_digest_config
            aux_job.requires = copy.deepcopy(docker_job_names)
            workflow.jobs.insert(len(docker_job_names), aux_job)
            docker_job_names.append(aux_job.name)

        assert docker_job_names, "Docker job names are empty, BUG?"
        for job in workflow.jobs[len(docker_job_names) :]:
            job.run_after.extend(docker_job_names)

    if workflow._enabled_workflow_config():
        from .native_jobs import _workflow_config_job

        if not _is_local_run():
            print(
                f"Enable native job [{_workflow_config_job.name}] for [{workflow.name}]"
            )
        aux_job = copy.deepcopy(_workflow_config_job)
        aux_job.runs_on = _native_job_runs_on(workflow)
        if workflow.config_job_timeout:
            aux_job.timeout = workflow.config_job_timeout
        workflow.jobs.insert(0, aux_job)
        for job in workflow.jobs[1:]:
            job.run_after.append(aux_job.name)

    if (
        workflow.enable_merge_ready_status
        or workflow.post_hooks
        or workflow.enable_automerge
        or workflow.enable_cidb  # to write cpu and storage usage summary into cidb
    ):
        from .native_jobs import _final_job

        if not _is_local_run():
            print(f"Enable native job [{_final_job.name}] for [{workflow.name}]")
        aux_job = copy.deepcopy(_final_job)
        aux_job.runs_on = _native_job_runs_on(workflow)
        for job in workflow.jobs:
            aux_job.run_after.append(job.name)
        workflow.jobs.append(aux_job)

    # Apply deferred cortage links now that all native jobs exist in the mangled
    # workflow. A member's attach_to(...) may configure the whole chain from one
    # place — multi-level predecessors and `then=` successors — including
    # Praktika-native jobs (Config/Docker/Finish) injected above that can't call
    # attach_to() themselves. Those extra (job, attach_to, promote) links are
    # carried on Job.Config.cortage_links and wired here. The validator's
    # deadlock/fork checks run on the un-mangled workflow and don't see native
    # jobs, so the structural checks for these links live here.
    jobs_by_name = {job.name: job for job in workflow.jobs}
    for src in workflow.jobs:
        for link in getattr(src, "cortage_links", None) or []:
            link_job, link_attach_to, link_promote = link
            if link_job not in jobs_by_name:
                Utils.raise_with_error(
                    f"Workflow [{workflow.name}]: job [{src.name}] attach_to wires "
                    f"unknown job [{link_job}] (jobs present: {sorted(jobs_by_name)})."
                )
            if link_attach_to not in jobs_by_name:
                Utils.raise_with_error(
                    f"Workflow [{workflow.name}]: job [{src.name}] attach_to wires "
                    f"[{link_job}] onto unknown job [{link_attach_to}]."
                )
            if link_job == link_attach_to:
                Utils.raise_with_error(
                    f"Workflow [{workflow.name}]: job [{src.name}] attach_to wires "
                    f"[{link_job}] onto itself."
                )
            target = jobs_by_name[link_job]
            # A later link must not silently contradict an earlier one (e.g. two
            # members both claiming the same native job with a different predecessor).
            if target.attached_to and target.attached_to != link_attach_to:
                Utils.raise_with_error(
                    f"Workflow [{workflow.name}]: conflicting cortage links for "
                    f"[{link_job}] — attached to both [{target.attached_to}] and "
                    f"[{link_attach_to}]."
                )
            target.attached_to = link_attach_to
            if link_promote:
                target.promote_runs_on = True

    # Finish Workflow is the orchestrator-owned summarizer: it reads the workflow
    # summary the orchestrator writes to S3 only once every job's row is finalized,
    # so it must run strictly after the orchestrator has observed every job
    # complete. Colocating it as a cortage member runs it on the chain driver right
    # after its predecessor — before the orchestrator's completion sweep has
    # recorded that predecessor — so Finish sees that job as not-finalized and
    # wrongly stamps it. Reject Finish anywhere in a cortage (as a member, or as a
    # predecessor something attaches to). Config and Docker builds colocate fine.
    finish_name = Settings.FINISH_WORKFLOW_JOB_NAME
    for job in workflow.jobs:
        if job.name == finish_name and getattr(job, "attached_to", ""):
            Utils.raise_with_error(
                f"Workflow [{workflow.name}]: [{finish_name}] cannot be a cortage "
                f"member (attached to [{job.attached_to}]). Finish summarizes the "
                f"whole run from the orchestrator-written state and must run after "
                f"every job is finalized; colocating it on a runner races that."
            )
        if getattr(job, "attached_to", "") == finish_name:
            Utils.raise_with_error(
                f"Workflow [{workflow.name}]: job [{job.name}] cannot attach to "
                f"[{finish_name}] — nothing may run after Finish on a shared runner; "
                f"Finish is the final orchestrator-owned summarizer."
            )

    # Structural cortage checks on the MANGLED graph. validate_cortages() runs on
    # the un-mangled workflow, so it can't see native jobs or applied cortage_links;
    # re-check the parts that only become visible here. The attach-target existence
    # check matters because the validator accepts any _native_job_names() member,
    # but a native job is only injected when its enabling conditions hold (e.g. a
    # Docker build needs workflow.dockers) — attaching to one that was never
    # injected would otherwise crash later (cortage_effective_runs_on KeyError).
    # TODO(cortage): also re-run the attach-cycle / dependency-cycle checks on the
    # mangled graph for links that close a cycle through native jobs.
    attached = [j for j in workflow.jobs if getattr(j, "attached_to", "")]
    for job in attached:
        if job.attached_to not in jobs_by_name:
            Utils.raise_with_error(
                f"Workflow [{workflow.name}]: job [{job.name}] attaches to "
                f"[{job.attached_to}], which is not a job in this workflow. A native "
                f"job (Config/Docker/Finish) is only a valid target when it is "
                f"actually injected here (e.g. a Docker build needs workflow.dockers)."
            )
    from collections import Counter

    for pred, count in Counter(j.attached_to for j in attached).items():
        if count > 1:
            forks = [j.name for j in attached if j.attached_to == pred]
            Utils.raise_with_error(
                f"Workflow [{workflow.name}]: jobs {forks} all attach to [{pred}]; a "
                f"cortage is a linked list, so at most one job may attach to a given "
                f"predecessor."
            )

    if workflow.runs_on_label_prefix:
        prefix = workflow.runs_on_label_prefix
        for job in workflow.jobs:
            if not job.runs_on:
                continue
            job.runs_on = [
                label if label == "self-hosted" else f"{prefix}{label}"
                for label in job.runs_on
            ]
