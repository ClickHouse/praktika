import os

from pathlib import Path

from praktika.infrastructure.lambda_function import Lambda
from praktika.infrastructure.report_page import ReportPage

# SSM paths AWS maintains with the latest AL2023 AMI IDs per region
_AL2023_ARM64_SSM_PATH = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-arm64"
_AL2023_X86_64_SSM_PATH = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"


def resolve_al2023_arm64_ami(region: str) -> str:
    """Resolve the latest AL2023 ARM64 AMI ID for the given region via AWS SSM."""
    from praktika.infrastructure._utils import aws_client
    ssm = aws_client("ssm", region, "ami-lookup")
    value = ssm.get_parameter(Name=_AL2023_ARM64_SSM_PATH)["Parameter"]["Value"]
    print(f"Resolved AL2023 ARM64 AMI for {region}: {value}")
    return value


def resolve_al2023_x86_64_ami(region: str) -> str:
    """Resolve the latest AL2023 x86_64 AMI ID for the given region via AWS SSM."""
    from praktika.infrastructure._utils import aws_client
    ssm = aws_client("ssm", region, "ami-lookup")
    value = ssm.get_parameter(Name=_AL2023_X86_64_SSM_PATH)["Parameter"]["Value"]
    print(f"Resolved AL2023 x86_64 AMI for {region}: {value}")
    return value

RUNNER_ROLE_NAME = "praktika-runner-role"
RUNNER_INSTANCE_PROFILE_NAME = "praktika-runner-profile"

ORCHESTRATOR_ROLE_NAME = "praktika-workflow-orchestrator-role"
ORCHESTRATOR_INSTANCE_PROFILE_NAME = "praktika-workflow-orchestrator-profile"

report_page_config = ReportPage.Config(
    path=str(Path(__file__).parent.parent.parent / "json.html"),
)

GH_TRIGGER_ROLE_NAME = "praktika-gh-trigger-role"
GH_TRIGGER_WEBHOOK_SECRET_NAME = "praktika-gh-trigger-webhook-secret"

lambda_gh_trigger_config = Lambda.Config(
    name="praktika-gh-trigger",
    path=f"{os.path.dirname(__file__)}/lambda_gh_trigger.py",
    handler="lambda_gh_trigger.lambda_handler",
    role_name=GH_TRIGGER_ROLE_NAME,
    secrets={
        GH_TRIGGER_WEBHOOK_SECRET_NAME: "GH_WEBHOOK_SECRET",
    },
    timeout_ms=10 * 1000,
    memory_size_mb=128,
    api_gateway=True,
)
