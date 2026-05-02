from .cidb_cluster import CIDBCluster
from .configs import lambda_gh_trigger_config, report_page_config
from .orchestrator_pool import OrchestratorPool
from .runner_pool import RunnerPool
from .user_data import ci_engine_user_data, cidb_user_data, runner_user_data


class NativeComponents:
    CIDBCluster = CIDBCluster
    OrchestratorPool = OrchestratorPool
    RunnerPool = RunnerPool
    report_page_config = report_page_config
    lambda_gh_trigger_config = lambda_gh_trigger_config
    # TODO: refactor, move to RunnerPool?
    ci_engine_user_data = staticmethod(ci_engine_user_data)
    runner_user_data = staticmethod(runner_user_data)
    cidb_user_data = staticmethod(cidb_user_data)
