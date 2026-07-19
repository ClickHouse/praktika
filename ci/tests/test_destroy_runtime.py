from types import SimpleNamespace

import pytest

from praktika.__main__ import create_parser
from praktika.infrastructure.cloud import CloudInfrastructure


class _Paginator:
    def __init__(self, fn):
        self._fn = fn

    def paginate(self, **kwargs):
        return [self._fn(**kwargs)]


class OperationNotPageableError(Exception):
    pass


class _OperationShape:
    def __init__(self, members):
        self.members = {name: object() for name in members}


class _OperationModel:
    def __init__(self, input_members):
        self.input_shape = _OperationShape(input_members)


class _ServiceModel:
    def operation_model(self, operation):
        if operation == "ListImageRecipes":
            return _OperationModel(["owner", "filters", "maxResults", "nextToken"])
        return _OperationModel([])


class _ClientMeta:
    method_to_api_mapping = {"list_image_recipes": "ListImageRecipes"}
    service_model = _ServiceModel()


class _FakeAWS:
    def __init__(self, calls):
        self.calls = calls
        self.clients = {
            "autoscaling": _FakeAutoscaling(calls),
            "ec2": _FakeEC2(calls),
            "sqs": _FakeSQS(calls),
            "events": _FakeEvents(calls),
            "lambda": _FakeLambda(calls),
            "apigatewayv2": _FakeAPIGateway(calls),
            "imagebuilder": _FakeImageBuilder(calls),
            "iam": _FakeIAM(calls),
            "ssm": _FakeSSM(calls),
            "s3": _FakeS3(calls),
        }

    def client(self, service, region, name):
        self.calls.append(f"client:{service}:{name}")
        return self.clients[service]


class _FakeAutoscaling:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def describe_auto_scaling_groups(self):
        return {
            "AutoScalingGroups": [
                {
                    "AutoScalingGroupName": "cloud_ci_infra-runner",
                    "Instances": [{"InstanceId": "i-runner"}],
                },
                {"AutoScalingGroupName": "other-runner"},
            ]
        }

    def delete_auto_scaling_group(self, AutoScalingGroupName, ForceDelete):
        self.calls.append(f"asg:{AutoScalingGroupName}")


class _FakeEC2:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def describe_launch_templates(self):
        return {
            "LaunchTemplates": [
                {"LaunchTemplateName": "cloud_ci_infra-runner-lt"},
                {"LaunchTemplateName": "other-runner-lt"},
            ]
        }

    def delete_launch_template(self, LaunchTemplateName):
        self.calls.append(f"lt:{LaunchTemplateName}")

    def describe_images(self, **kwargs):
        return {
            "Images": [
                {
                    "ImageId": "ami-runtime",
                    "Name": "cloud_ci_infra-runner-20240101",
                    "BlockDeviceMappings": [
                        {"Ebs": {"SnapshotId": "snap-runtime"}}
                    ],
                },
                {
                    "ImageId": "ami-other",
                    "Name": "other-runner-20240101",
                },
            ]
        }

    def deregister_image(self, ImageId):
        self.calls.append(f"ami:{ImageId}")

    def delete_snapshot(self, SnapshotId):
        self.calls.append(f"snapshot:{SnapshotId}")

    def describe_instances(self, **kwargs):
        self.calls.append("ec2:describe_instances")
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-runner",
                            "Tags": [
                                {"Key": "Name", "Value": "cloud_ci_infra-runner"}
                            ],
                        },
                        {
                            "InstanceId": "i-cidb",
                            "Tags": [
                                {"Key": "Name", "Value": "cloud_ci_infra-cidb-01"}
                            ],
                        }
                    ]
                }
            ]
        }

    def terminate_instances(self, InstanceIds):
        self.calls.append(f"ec2:terminate:{','.join(InstanceIds)}")

    def describe_hosts(self):
        return {
            "Hosts": [
                {
                    "HostId": "h-runtime",
                    "Tags": [{"Key": "Name", "Value": "cloud_ci_infra-host"}],
                }
            ]
        }

    def release_hosts(self, HostIds):
        self.calls.append(f"host:{','.join(HostIds)}")

    def describe_vpcs(self, **kwargs):
        return {
            "Vpcs": [
                {
                    "VpcId": "vpc-runtime",
                    "Tags": [{"Key": "Name", "Value": "cloud_ci_infra-vpc"}],
                },
                {
                    "VpcId": "vpc-other",
                    "Tags": [{"Key": "Name", "Value": "other-vpc"}],
                },
            ]
        }

    def describe_tags(self, Filters):
        resource_types = next(
            (f["Values"] for f in Filters if f["Name"] == "resource-type"),
            [],
        )
        names = next((f["Values"] for f in Filters if f["Name"] == "tag:Name"), [])
        if resource_types == ["vpc"] and names == ["cloud_ci_infra-vpc"]:
            return {"Tags": [{"ResourceId": "vpc-runtime"}]}
        return {"Tags": []}

    def describe_subnets(self, Filters):
        return {"Subnets": [{"SubnetId": "subnet-runtime"}]}

    def delete_subnet(self, SubnetId):
        self.calls.append(f"subnet:{SubnetId}")

    def describe_route_tables(self, Filters):
        return {
            "RouteTables": [
                {
                    "RouteTableId": "rtb-runtime",
                    "Associations": [{"Main": False}],
                },
                {
                    "RouteTableId": "rtb-main",
                    "Associations": [{"Main": True}],
                },
            ]
        }

    def delete_route_table(self, RouteTableId):
        self.calls.append(f"rt:{RouteTableId}")

    def describe_internet_gateways(self, Filters):
        return {"InternetGateways": [{"InternetGatewayId": "igw-runtime"}]}

    def detach_internet_gateway(self, InternetGatewayId, VpcId):
        self.calls.append(f"igw-detach:{InternetGatewayId}:{VpcId}")

    def delete_internet_gateway(self, InternetGatewayId):
        self.calls.append(f"igw:{InternetGatewayId}")

    def describe_security_groups(self, Filters):
        return {
            "SecurityGroups": [
                {"GroupId": "sg-default", "GroupName": "default"},
                {"GroupId": "sg-runtime", "GroupName": "cloud_ci_infra-vpc-sg"},
            ]
        }

    def delete_security_group(self, GroupId):
        self.calls.append(f"sg:{GroupId}")

    def delete_vpc(self, VpcId):
        self.calls.append(f"vpc:{VpcId}")


class _FakeSQS:
    def __init__(self, calls):
        self.calls = calls

    def list_queues(self, QueueNamePrefix):
        return {
            "QueueUrls": [
                f"https://sqs.test/123/{QueueNamePrefix}runner",
                f"https://sqs.test/123/{QueueNamePrefix}runner-dlq",
            ]
        }

    def delete_queue(self, QueueUrl):
        self.calls.append(f"sqs:{QueueUrl.rsplit('/', 1)[-1]}")


class _FakeEvents:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def list_rules(self, NamePrefix):
        return {"Rules": [{"Name": f"{NamePrefix}pool-autoscaler-schedule"}]}

    def list_targets_by_rule(self, Rule):
        return {"Targets": [{"Id": f"{Rule}-target"}]}

    def remove_targets(self, Rule, Ids, Force):
        self.calls.append(f"event-targets:{Rule}")

    def delete_rule(self, Name, Force):
        self.calls.append(f"event:{Name}")


class _FakeLambda:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def list_functions(self):
        return {
            "Functions": [
                {"FunctionName": "cloud_ci_infra-pool-autoscaler"},
                {"FunctionName": "cloud_ci_infra-gh-token"},
                {"FunctionName": "cloud_ci_infra-workflow-orchestrator"},
                {"FunctionName": "other-workflow-orchestrator"},
            ]
        }

    def get_function(self, FunctionName):
        return {
            "Configuration": {
                "Role": (
                    "arn:aws:iam::123:role/"
                    f"{FunctionName.replace('workflow-orchestrator', 'gh-trigger-role')}"
                )
            }
        }

    def delete_function(self, FunctionName):
        self.calls.append(f"lambda:{FunctionName}")


class _FakeAPIGateway:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def get_apis(self):
        return {
            "Items": [
                {
                    "Name": "cloud_ci_infra-workflow-orchestrator-API",
                    "ApiId": "api-1",
                }
            ]
        }

    def delete_api(self, ApiId):
        self.calls.append(f"api:{ApiId}")


class _FakeImageBuilder:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def list_image_pipelines(self):
        return {
            "imagePipelineList": [
                {"name": "cloud_ci_infra-image", "arn": "arn:pipeline"}
            ]
        }

    def delete_image_pipeline(self, imagePipelineArn):
        self.calls.append(f"ib-pipeline:{imagePipelineArn}")

    def list_image_recipes(self):
        return {"imageRecipeSummaryList": []}

    def delete_image_recipe(self, imageRecipeArn):
        self.calls.append(f"ib-recipe:{imageRecipeArn}")

    def list_distribution_configurations(self):
        return {"distributionConfigurationSummaryList": []}

    def list_infrastructure_configurations(self):
        return {"infrastructureConfigurationSummaryList": []}

    def list_components(self, owner):
        return {"componentVersionList": []}


class _FakeImageBuilderWithoutPaginator(_FakeImageBuilder):
    meta = _ClientMeta()

    def get_paginator(self, op):
        raise OperationNotPageableError()

    def list_image_recipes(self, **request):
        self.calls.append(f"ib-recipe-request:{','.join(sorted(request))}")
        if "NextToken" in request:
            raise AssertionError("Image Builder list_image_recipes expects nextToken")
        if request.get("nextToken") == "page-2":
            return {"imageRecipeSummaryList": []}
        return {
            "imageRecipeSummaryList": [
                {"name": "cloud_ci_infra-recipe", "arn": "arn:recipe"}
            ],
            "nextToken": "page-2",
        }


class _FakeImageBuilderWithRecipeVersions(_FakeImageBuilder):
    def list_image_recipes(self):
        return {
            "imageRecipeSummaryList": [
                {
                    "name": "cloud_ci_infra-image-recipe",
                    "arn": "arn:aws:imagebuilder:test:123:image-recipe/cloud_ci_infra-image-recipe/1.0.0",
                    "semanticVersion": "1.0.0",
                },
                {
                    "name": "cloud_ci_infra-image-recipe",
                    "arn": "arn:aws:imagebuilder:test:123:image-recipe/cloud_ci_infra-image-recipe/1.0.1",
                    "semanticVersion": "1.0.1",
                },
            ]
        }


class _FakeImageBuilderWithComponentBuilds(_FakeImageBuilder):
    def list_components(self, owner):
        return {
            "componentVersionList": [
                {
                    "name": "cloud_ci_infra-praktika-runtime-venv",
                    "arn": "arn:aws:imagebuilder:test:123:component/cloud_ci_infra-praktika-runtime-venv/1.0.0",
                    "version": "1.0.0",
                }
            ]
        }

    def list_component_build_versions(self, componentVersionArn):
        self.calls.append(f"ib-component-version:{componentVersionArn}")
        return {
            "componentSummaryList": [
                {
                    "name": "cloud_ci_infra-praktika-runtime-venv",
                    "arn": f"{componentVersionArn}/1",
                    "version": "1.0.0",
                }
            ]
        }

    def delete_component(self, componentBuildVersionArn):
        self.calls.append(f"ib-component:{componentBuildVersionArn}")


class _FakeIAM:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def list_instance_profiles(self):
        return {
            "InstanceProfiles": [
                {
                    "InstanceProfileName": "cloud_ci_infra-runner-profile",
                    "Roles": [{"RoleName": "cloud_ci_infra-runner-role"}],
                },
                {
                    "InstanceProfileName": "cloud_ci_infra-cidb-profile",
                    "Roles": [{"RoleName": "cloud_ci_infra-cidb-role"}],
                },
            ]
        }

    def remove_role_from_instance_profile(self, InstanceProfileName, RoleName):
        self.calls.append(f"profile-remove:{InstanceProfileName}:{RoleName}")

    def delete_instance_profile(self, InstanceProfileName):
        self.calls.append(f"profile:{InstanceProfileName}")

    def list_roles(self):
        return {
            "Roles": [
                {"RoleName": "cloud_ci_infra-runner-role"},
                {"RoleName": "cloud_ci_infra-pool-autoscaler-role"},
                {"RoleName": "cloud_ci_infra-gh-token-role"},
                {"RoleName": "cloud_ci_infra-gh-trigger-role"},
                {"RoleName": "cloud_ci_infra-cidb-role"},
            ]
        }

    def list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": []}

    def list_role_policies(self, RoleName):
        return {"PolicyNames": []}

    def delete_role(self, RoleName):
        self.calls.append(f"role:{RoleName}")


class _FakeSSM:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def describe_parameters(self, ParameterFilters):
        prefix = ParameterFilters[0]["Values"][0]
        return {"Parameters": [{"Name": f"{prefix}secret"}]}

    def delete_parameter(self, Name):
        self.calls.append(f"ssm:{Name}")


class _FakeS3:
    def __init__(self, calls):
        self.calls = calls

    def get_paginator(self, op):
        return _Paginator(getattr(self, op))

    def list_buckets(self):
        return {"Buckets": [{"Name": "cloud_ci_infra-artifacts"}]}

    def list_objects_v2(self, Bucket):
        return {"Contents": []}

    def delete_bucket(self, Bucket):
        self.calls.append(f"s3:{Bucket}")


def _cloud(monkeypatch):
    calls = []
    fake = _FakeAWS(calls)
    monkeypatch.setattr("praktika.infrastructure.cloud.aws_client", fake.client)
    monkeypatch.setattr("praktika.infrastructure.vpc.aws_client", fake.client)
    cloud = CloudInfrastructure.Config(name="cloud_ci_infra")
    cloud._settings = SimpleNamespace(AWS_REGION="eu-north-1")
    monkeypatch.setattr(cloud, "_verify_account", lambda: None)

    from praktika.interactive import UserPrompt

    monkeypatch.setattr(UserPrompt, "confirm", staticmethod(lambda _: True))
    return cloud, calls


def test_infrastructure_parser_supports_destroy_runtime():
    parser = create_parser()
    args = parser.parse_args(["infrastructure", "--destroy-runtime"])

    assert args.command == "infrastructure"
    assert args.destroy_runtime is True
    assert args.destroy_all is False
    assert args.deploy is False
    assert args.restart_instances is False


def test_infrastructure_parser_supports_destroy_all():
    parser = create_parser()
    args = parser.parse_args(["infrastructure", "--destroy-all", "--project", "cloud_ci_infra"])

    assert args.command == "infrastructure"
    assert args.destroy_all is True
    assert args.project == "cloud_ci_infra"


def test_infrastructure_parser_supports_yes():
    parser = create_parser()
    args = parser.parse_args(["infrastructure", "--destroy-runtime", "-y"])

    assert args.command == "infrastructure"
    assert args.destroy_runtime is True
    assert args.yes is True


def test_destroy_runtime_removes_recreatable_resources_but_keeps_stateful_ones(monkeypatch):
    cloud, calls = _cloud(monkeypatch)

    cloud.destroy_runtime()

    assert "asg:cloud_ci_infra-runner" in calls
    assert "lt:cloud_ci_infra-runner-lt" in calls
    assert "sqs:cloud_ci_infra-runner" in calls
    assert "event:cloud_ci_infra-pool-autoscaler-schedule" in calls
    assert "lambda:cloud_ci_infra-pool-autoscaler" in calls
    assert "lambda:cloud_ci_infra-gh-token" in calls
    assert "lambda:cloud_ci_infra-workflow-orchestrator" in calls
    assert "ib-pipeline:arn:pipeline" in calls
    assert "ami:ami-runtime" in calls
    assert "snapshot:snap-runtime" in calls
    assert "profile:cloud_ci_infra-runner-profile" in calls
    assert "role:cloud_ci_infra-runner-role" in calls
    assert "vpc:vpc-runtime" in calls
    assert "role:cloud_ci_infra-gh-trigger-role" not in calls
    assert "profile:cloud_ci_infra-cidb-profile" not in calls
    assert "role:cloud_ci_infra-cidb-role" not in calls
    assert "ec2:describe_instances" not in calls
    assert "api:api-1" not in calls
    assert "host:h-runtime" not in calls
    assert "ssm:cloud_ci_infra-secret" not in calls
    assert "ssm:/cloud_ci_infra-secret" not in calls
    assert "s3:cloud_ci_infra-artifacts" not in calls


def test_destroy_all_expands_to_project_prefixed_stateful_and_webhook_resources(monkeypatch):
    cloud, calls = _cloud(monkeypatch)

    cloud.destroy_all()

    assert "lambda:cloud_ci_infra-workflow-orchestrator" in calls
    assert "api:api-1" in calls
    assert "ec2:describe_instances" in calls
    assert "ec2:terminate:i-runner" not in calls
    assert "ec2:terminate:i-cidb" in calls
    assert "profile:cloud_ci_infra-cidb-profile" in calls
    assert "role:cloud_ci_infra-cidb-role" in calls
    assert "host:h-runtime" in calls
    assert "ssm:cloud_ci_infra-secret" in calls
    assert "ssm:/cloud_ci_infra-secret" in calls
    assert "s3:cloud_ci_infra-artifacts" in calls


def test_destroy_all_does_not_prompt_for_instances_owned_by_deleted_asgs(monkeypatch):
    cloud, calls = _cloud(monkeypatch)
    prompts = []

    from praktika.interactive import UserPrompt

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda prompt: prompts.append(prompt) or True),
    )

    cloud.destroy_all()

    assert "Delete 'AutoScalingGroup cloud_ci_infra-runner'?" in prompts
    assert "Delete 'EC2Instance cloud_ci_infra-runner (i-runner)'?" not in prompts
    assert "Delete 'EC2Instance cloud_ci_infra-cidb-01 (i-cidb)'?" in prompts
    assert "ec2:terminate:i-runner" not in calls
    assert "ec2:terminate:i-cidb" in calls


def test_destroy_runtime_batches_sqs_confirmation(monkeypatch):
    cloud, calls = _cloud(monkeypatch)
    prompts = []

    from praktika.interactive import UserPrompt

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda prompt: prompts.append(prompt) or True),
    )

    cloud.destroy_runtime(only=["SQS"])

    assert prompts == ["Delete all 2 SQS queues?"]
    assert "sqs:cloud_ci_infra-runner" in calls
    assert "sqs:cloud_ci_infra-runner-dlq" in calls


def test_destroy_runtime_only_images_deletes_amis_without_imagebuilder(monkeypatch):
    cloud, calls = _cloud(monkeypatch)

    cloud.destroy_runtime(only=["Images"])

    assert "ami:ami-runtime" in calls
    assert "snapshot:snap-runtime" in calls
    assert "ib-pipeline:arn:pipeline" not in calls
    assert "asg:cloud_ci_infra-runner" not in calls


def test_destroy_runtime_imagebuilder_fallback_uses_lowercase_next_token(monkeypatch):
    cloud, calls = _cloud(monkeypatch)
    calls.clear()

    fake = _FakeAWS(calls)
    fake.clients["imagebuilder"] = _FakeImageBuilderWithoutPaginator(calls)
    monkeypatch.setattr("praktika.infrastructure.cloud.aws_client", fake.client)

    cloud.destroy_runtime(only=["ImageBuilder"])

    assert "ib-recipe-request:" in calls
    assert "ib-recipe-request:nextToken" in calls
    assert "ib-recipe:arn:recipe" in calls


def test_destroy_runtime_imagebuilder_recipe_prompts_batch_versions(monkeypatch):
    cloud, calls = _cloud(monkeypatch)
    calls.clear()
    prompts = []

    fake = _FakeAWS(calls)
    fake.clients["imagebuilder"] = _FakeImageBuilderWithRecipeVersions(calls)
    monkeypatch.setattr("praktika.infrastructure.cloud.aws_client", fake.client)

    from praktika.interactive import UserPrompt

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda prompt: prompts.append(prompt) or True),
    )

    cloud.destroy_runtime(only=["ImageBuilder"])

    assert (
        "Delete all 2 versions of ImageBuilderRecipe cloud_ci_infra-image-recipe?"
        in prompts
    )
    assert (
        "Delete 'ImageBuilderRecipe cloud_ci_infra-image-recipe (1.0.0)'?"
        not in prompts
    )
    assert (
        "Delete 'ImageBuilderRecipe cloud_ci_infra-image-recipe (1.0.1)'?"
        not in prompts
    )
    assert (
        "ib-recipe:arn:aws:imagebuilder:test:123:image-recipe/cloud_ci_infra-image-recipe/1.0.0"
        in calls
    )
    assert (
        "ib-recipe:arn:aws:imagebuilder:test:123:image-recipe/cloud_ci_infra-image-recipe/1.0.1"
        in calls
    )


def test_destroy_runtime_imagebuilder_deletes_component_build_versions(monkeypatch):
    cloud, calls = _cloud(monkeypatch)
    calls.clear()
    prompts = []

    fake = _FakeAWS(calls)
    fake.clients["imagebuilder"] = _FakeImageBuilderWithComponentBuilds(calls)
    monkeypatch.setattr("praktika.infrastructure.cloud.aws_client", fake.client)

    from praktika.interactive import UserPrompt

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda prompt: prompts.append(prompt) or True),
    )

    cloud.destroy_runtime(only=["ImageBuilder"])

    component_version_arn = (
        "arn:aws:imagebuilder:test:123:component/"
        "cloud_ci_infra-praktika-runtime-venv/1.0.0"
    )
    assert f"ib-component-version:{component_version_arn}" in calls
    assert f"ib-component:{component_version_arn}/1" in calls
    assert (
        "Delete 'ImageBuilderComponent cloud_ci_infra-praktika-runtime-venv (1.0.0/1)'?"
        in prompts
    )


def test_infrastructure_main_destroy_requires_project(monkeypatch):
    from praktika.__main__ import main

    monkeypatch.setattr(
        "praktika.mangle._get_infra_config",
        lambda project, require_project=False: (_ for _ in ()).throw(
            RuntimeError("project required")
        ),
    )

    with pytest.raises(RuntimeError, match="project required"):
        main(["infrastructure", "--destroy-runtime"])


def test_infrastructure_main_rejects_destroy_runtime_all(monkeypatch):
    from praktika.__main__ import main

    with pytest.raises(RuntimeError, match="Use --destroy-all"):
        main(["infrastructure", "--destroy-runtime", "--all", "--project", "cloud_ci_infra"])


def test_infrastructure_main_deploy_validates_before_deploy(monkeypatch):
    from praktika.__main__ import main
    from praktika.validator import Validator

    calls = []

    class _Config:
        def deploy(self, **kwargs):
            calls.append(("deploy", kwargs))

    config = _Config()

    monkeypatch.setattr(
        "praktika.mangle._get_infra_config",
        lambda project, require_project=False: config,
    )
    monkeypatch.setattr(
        Validator,
        "validate_infrastructure_deploy",
        lambda self, cloud: calls.append(("validate", cloud)),
    )

    main(["infrastructure", "--deploy", "--project", "cloud_ci_infra"])

    assert calls == [
        ("validate", config),
        ("deploy", {"all": False, "only": None, "is_test": False}),
    ]


def test_infrastructure_main_yes_enables_auto_confirm(monkeypatch):
    from praktika.__main__ import main
    from praktika.interactive import UserPrompt

    previous = UserPrompt.AUTO_CONFIRM
    seen = {"auto_confirm": None}

    class _Config:
        def destroy_runtime(self, **kwargs):
            seen["auto_confirm"] = UserPrompt.AUTO_CONFIRM

    monkeypatch.setattr(
        "praktika.mangle._get_infra_config",
        lambda project, require_project=False: _Config(),
    )

    try:
        main([
            "infrastructure",
            "--destroy-runtime",
            "--project",
            "cloud_ci_infra",
            "--yes",
        ])
    finally:
        UserPrompt.AUTO_CONFIRM = previous

    assert seen["auto_confirm"] is True
