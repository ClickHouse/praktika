from praktika.infrastructure.image_builder import ImageBuilder
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.runner_pool import RunnerPool


def test_runner_pool_registers_launch_template_with_image_builder():
    builder = ImageBuilder.Config(
        name="runner-arm64-image",
        image_pipeline_name="runner-arm64-imagebuilder-pipeline",
    )

    pool = RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Disabled,
        size=1,
        max_size=1,
        image_builder=builder,
    )

    assert builder.launch_templates == [pool.launch_template]
    assert pool.launch_template.image_builder is builder
    assert pool.autoscaling_group.launch_template_version == "$Default"


def test_orchestrator_pool_registers_launch_template_with_image_builder():
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        image_pipeline_name="orchestrator-arm64-imagebuilder-pipeline",
    )

    pool = OrchestratorPool(
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        image_builder=builder,
    )

    assert builder.launch_templates == [pool.launch_template]
    assert pool.launch_template.image_builder is builder
    assert pool.autoscaling_group.launch_template_version == "$Default"


def test_launch_template_resolves_latest_ami_from_image_builder(monkeypatch):
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
    )

    def _resolve_latest_ami_id():
        assert builder.region == "eu-north-1"
        return "ami-0123456789abcdef0"

    monkeypatch.setattr(builder, "resolve_latest_ami_id", _resolve_latest_ami_id)

    pool = OrchestratorPool(
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        image_builder=builder,
    )

    pool.launch_template.region = "eu-north-1"
    assert pool.launch_template._resolve_image_id() == "ami-0123456789abcdef0"


def test_image_builder_distribution_includes_launch_templates(monkeypatch):
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        region="eu-north-1",
        distribution_configuration_name="praktika-orchestrator-dist",
        ami_name="praktika-orchestrator-{{ imagebuilder:buildDate }}",
        ami_launch_permission={"userGroups": ["all"]},
        regions=["eu-north-1"],
    )

    pool = OrchestratorPool(
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        image_builder=builder,
    )

    def _fetch():
        pool.launch_template.ext["launch_template_id"] = "lt-0123456789abcdef0"
        return pool.launch_template

    monkeypatch.setattr(pool.launch_template, "fetch", _fetch)

    captured = {}

    class _Client:
        def create_distribution_configuration(self, **req):
            captured.update(req)
            return {
                "distributionConfigurationArn": "arn:aws:imagebuilder:eu-north-1:123456789012:distribution-configuration/praktika-orchestrator-dist"
            }

        def update_distribution_configuration(self, **req):
            captured.update(req)
            return {}

    monkeypatch.setattr(builder, "_client", lambda: _Client())

    arn = builder._get_or_create_distribution_configuration_arn()

    assert arn.endswith("/praktika-orchestrator-dist")
    assert captured["distributions"][0]["launchTemplateConfigurations"] == [
        {
            "launchTemplateId": "lt-0123456789abcdef0",
            "setDefaultVersion": True,
        }
    ]
    assert captured["distributions"][0]["amiDistributionConfiguration"][
        "launchPermission"
    ] == {"userGroups": ["all"]}


def test_image_builder_pipeline_update_is_skipped_when_unchanged(monkeypatch):
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        region="eu-north-1",
        image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
        enabled=True,
        schedule_expression="rate(1 minute)",
    )

    update_called = {"value": False}

    class _Client:
        def create_image_pipeline(self, **req):
            class ResourceAlreadyExistsException(Exception):
                pass

            raise ResourceAlreadyExistsException()

        def get_image_pipeline(self, imagePipelineArn):
            return {
                "imagePipeline": {
                    "imageRecipeArn": "arn:recipe",
                    "infrastructureConfigurationArn": "arn:infra",
                    "distributionConfigurationArn": "arn:dist",
                    "status": "ENABLED",
                    "schedule": {
                        "scheduleExpression": "rate(1 minute)",
                        "pipelineExecutionStartCondition": "EXPRESSION_MATCH_ONLY",
                    },
                }
            }

        def update_image_pipeline(self, **req):
            update_called["value"] = True
            return {}

    monkeypatch.setattr(builder, "_client", lambda: _Client())
    monkeypatch.setattr(
        builder,
        "_imagebuilder_arn",
        lambda resource_type, name: f"arn:{resource_type}:{name}",
    )

    arn = builder._get_or_create_pipeline_arn("arn:recipe", "arn:infra", "arn:dist")

    assert arn == "arn:image-pipeline:praktika-orchestrator-arm64-imagebuilder-pipeline"
    assert update_called["value"] is False


def test_image_builder_reuses_existing_inline_component_when_create_conflicts(
    monkeypatch,
):
    builder = ImageBuilder.Config(
        name="base-runner-x86_64-image",
        region="eu-north-1",
        inline_components=[
            {
                "name": "praktika-base-runner-runtime",
                "version": "1.0.0",
                "platform": "Linux",
                "commands": ["echo hello"],
            }
        ],
    )

    class _Client:
        def list_components(self, **req):
            return {"componentVersionList": []}

        def create_component(self, **req):
            class ResourceAlreadyExistsException(Exception):
                pass

            raise ResourceAlreadyExistsException()

    monkeypatch.setattr(builder, "_client", lambda: _Client())
    monkeypatch.setattr(
        builder,
        "_imagebuilder_arn",
        lambda resource_type, name: f"arn:{resource_type}:{name}",
    )

    arns = builder._ensure_inline_components()

    assert arns == ["arn:component:praktika-base-runner-runtime/1.0.0/1"]
