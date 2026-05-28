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
        scaling_type=RunnerPool.ScalingType.Fixed,
        size=1,
        max_size=1,
        image_builder=builder,
    )

    assert builder.launch_templates == [pool.launch_template]
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
    assert pool.autoscaling_group.launch_template_version == "$Default"


def test_image_builder_distribution_includes_launch_templates(monkeypatch):
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        region="eu-north-1",
        distribution_configuration_name="praktika-orchestrator-dist",
        ami_name="praktika-orchestrator-{{ imagebuilder:buildDate }}",
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
