from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import NativeComponents, Storage, VPC

CI_VPC_NAME = "praktika-ci"

_runner_pools = [
    NativeComponents.RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name=CI_VPC_NAME,
        scaling_type=NativeComponents.RunnerPool.ScalingType.Fixed,
        size=1,
        max_size=1,
    ),
    NativeComponents.RunnerPool(
        name="amd-2xsmall",
        instance_type="t3a.small",
        vpc_name=CI_VPC_NAME,
        scaling_type=NativeComponents.RunnerPool.ScalingType.Fixed,
        size=1,
        max_size=1,
    ),
]

_orchestrator_pool = NativeComponents.OrchestratorPool(
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    size=1,
    max_size=1,
)

_cidb_cluster = NativeComponents.CIDBCluster(
    vpc_name=CI_VPC_NAME,
    instance_type="t4g.large",
    size=1,
)

CLOUD = CloudInfrastructure.Config(
    name="cloud_ci_infra",
    vpcs=[
        VPC.Config(
            name=CI_VPC_NAME,
            subnets=[
                VPC.Subnet(availability_zone="eu-north-1a"),
            ],
        )
    ],
    storages=[
        Storage.Config(name="praktika-artifacts-eu-north-1", retention_days=90, public=True),
    ],
    report_pages=[
        NativeComponents.report_page_config,
    ],
    orchestrator_pool=_orchestrator_pool,
    runner_pools=_runner_pools,
    cidb_cluster=_cidb_cluster,
)
