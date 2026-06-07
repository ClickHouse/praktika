from praktika.infrastructure.image_builder import ImageBuilder
from praktika.infrastructure.launch_template import LaunchTemplate
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


def test_image_builder_distribution_skips_missing_launch_templates(monkeypatch):
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
        raise Exception("Launch Template 'workflow-orchestrator-lt' not found in AWS")

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
    assert "launchTemplateConfigurations" not in captured["distributions"][0]


def test_image_builder_distribution_reuses_cached_launch_template_id(monkeypatch):
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
    pool.launch_template.ext["launch_template_id"] = "lt-cached0123456789"

    def _fetch():
        raise AssertionError("fetch should not be called when launch_template_id is cached")

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
            "launchTemplateId": "lt-cached0123456789",
            "setDefaultVersion": True,
        }
    ]


def test_image_builder_delete_skips_dependent_components(monkeypatch):
    builder = ImageBuilder.Config(
        name="controller-image",
        region="eu-north-1",
        inline_components=[
            {
                "name": "praktika-controller-setup",
                "version": "1.0.10",
                "description": "test",
                "data": "name: test",
            }
        ],
    )

    class _Client:
        def list_image_pipelines(self, **req):
            return {"imagePipelineList": []}

        def list_image_recipes(self, **req):
            return {"imageRecipeSummaryList": []}

        def list_distribution_configurations(self, **req):
            return {"distributionConfigurationSummaryList": []}

        def list_infrastructure_configurations(self, **req):
            return {"infrastructureConfigurationSummaryList": []}

        def list_components(self, **req):
            return {
                "componentVersionList": [
                    {
                        "name": "praktika-controller-setup",
                        "semanticVersion": "1.0.10",
                        "arn": (
                            "arn:aws:imagebuilder:eu-north-1:123456789012:"
                            "component/praktika-controller-setup/1.0.10/1"
                        ),
                    }
                ]
            }

        def delete_component(self, **req):
            class ResourceDependencyException(Exception):
                pass

            raise ResourceDependencyException("Resource dependency error")

    monkeypatch.setattr(builder, "_client", lambda: _Client())

    builder.delete()


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


def test_launch_template_deploy_skips_when_image_builder_has_no_images(monkeypatch):
    lt = LaunchTemplate.Config(
        name="workflow-orchestrator-lt",
        region="eu-north-1",
        instance_type="t4g.small",
        image_builder=ImageBuilder.Config(
            name="ci-arm64-image",
            image_pipeline_name="ci-arm64-imagebuilder-pipeline",
        ),
    )

    class _EC2Client:
        pass

    monkeypatch.setattr(
        "praktika.infrastructure.launch_template.aws_client",
        lambda *args, **kwargs: _EC2Client(),
    )
    monkeypatch.setattr(
        lt,
        "fetch",
        lambda: (_ for _ in ()).throw(Exception("Launch Template not found")),
    )

    def _build():
        raise Exception(
            "No ready AMI found for Image Builder pipeline "
            "'ci-arm64-imagebuilder-pipeline'. Rerun deploy after the image is ready."
        )

    monkeypatch.setattr(lt, "_build_launch_template_data", _build)

    try:
        lt.deploy()
        assert False, "expected deploy to fail"
    except Exception as e:
        assert str(e) == (
            "Image Builder output is not ready yet for Launch Template "
            "'workflow-orchestrator-lt'. Rerun deploy after the image is ready."
        )


def test_image_builder_resolves_latest_ready_ami_not_latest_pending(monkeypatch):
    builder = ImageBuilder.Config(
        name="ci-arm64-image",
        region="eu-north-1",
        image_pipeline_name="ci-arm64-imagebuilder-pipeline",
    )

    class _Client:
        def list_image_pipelines(self, **req):
            return {
                "imagePipelineList": [
                    {
                        "name": "ci-arm64-imagebuilder-pipeline",
                        "arn": "arn:pipeline",
                    }
                ]
            }

        def list_image_pipeline_images(self, **req):
            return {
                "imageSummaryList": [
                    {"arn": "arn:image-new", "dateCreated": "2026-06-07T11:00:00Z"},
                    {"arn": "arn:image-old", "dateCreated": "2026-06-07T10:00:00Z"},
                ]
            }

        def get_image(self, imageBuildVersionArn):
            if imageBuildVersionArn == "arn:image-new":
                return {"image": {"outputResources": {"amis": []}}}
            if imageBuildVersionArn == "arn:image-old":
                return {
                    "image": {
                        "outputResources": {
                            "amis": [
                                {"region": "eu-north-1", "image": "ami-ready0123456789"}
                            ]
                        }
                    }
                }
            raise AssertionError(f"unexpected image arn {imageBuildVersionArn}")

    monkeypatch.setattr(builder, "_client", lambda: _Client())

    assert builder.resolve_latest_ami_id() == "ami-ready0123456789"


def test_launch_template_deploy_fails_when_latest_builds_have_no_ready_ami(monkeypatch):
    lt = LaunchTemplate.Config(
        name="workflow-orchestrator-lt",
        region="eu-north-1",
        instance_type="t4g.small",
        image_builder=ImageBuilder.Config(
            name="ci-arm64-image",
            image_pipeline_name="ci-arm64-imagebuilder-pipeline",
        ),
    )

    class _EC2Client:
        pass

    monkeypatch.setattr(
        "praktika.infrastructure.launch_template.aws_client",
        lambda *args, **kwargs: _EC2Client(),
    )
    monkeypatch.setattr(
        lt,
        "fetch",
        lambda: (_ for _ in ()).throw(Exception("Launch Template not found")),
    )

    def _build():
        raise Exception(
            "No ready AMI found for Image Builder pipeline "
            "'ci-arm64-imagebuilder-pipeline'. Rerun deploy after the image is ready."
        )

    monkeypatch.setattr(lt, "_build_launch_template_data", _build)

    try:
        lt.deploy()
        assert False, "expected deploy to fail"
    except Exception as e:
        assert str(e) == (
            "Image Builder output is not ready yet for Launch Template "
            "'workflow-orchestrator-lt'. Rerun deploy after the image is ready."
        )


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


def test_image_builder_deploy_starts_build_when_pipeline_changed(monkeypatch):
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        region="eu-north-1",
        image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
        enabled=True,
    )

    monkeypatch.setattr(builder, "fetch", lambda: builder)
    monkeypatch.setattr(builder, "_get_or_create_recipe_arn", lambda: "arn:recipe:new")
    monkeypatch.setattr(
        builder,
        "_get_or_create_infrastructure_configuration_arn",
        lambda: "arn:infra:new",
    )
    monkeypatch.setattr(
        builder,
        "_get_or_create_distribution_configuration_arn",
        lambda: "arn:dist:new",
    )
    monkeypatch.setattr(
        builder,
        "_get_or_create_pipeline_arn",
        lambda recipe, infra, dist: "arn:pipeline:new",
    )

    started = {}

    class _Client:
        def start_image_pipeline_execution(self, imagePipelineArn):
            started["arn"] = imagePipelineArn
            return {"imageBuildVersionArn": "arn:build:new"}

    monkeypatch.setattr(builder, "_client", lambda: _Client())

    result = builder.deploy()

    assert result is builder
    assert started["arn"] == "arn:pipeline:new"
    assert builder.ext["last_started_build_arn"] == "arn:build:new"


def test_image_builder_deploy_skips_build_when_unchanged(monkeypatch):
    builder = ImageBuilder.Config(
        name="orchestrator-arm64-image",
        region="eu-north-1",
        image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
        enabled=True,
    )
    builder.ext["image_recipe_arn"] = "arn:recipe:same"
    builder.ext["infrastructure_configuration_arn"] = "arn:infra:same"
    builder.ext["distribution_configuration_arn"] = "arn:dist:same"
    builder.ext["image_pipeline_arn"] = "arn:pipeline:same"

    monkeypatch.setattr(builder, "fetch", lambda: builder)
    monkeypatch.setattr(builder, "_get_or_create_recipe_arn", lambda: "arn:recipe:same")
    monkeypatch.setattr(
        builder,
        "_get_or_create_infrastructure_configuration_arn",
        lambda: "arn:infra:same",
    )
    monkeypatch.setattr(
        builder,
        "_get_or_create_distribution_configuration_arn",
        lambda: "arn:dist:same",
    )
    monkeypatch.setattr(
        builder,
        "_get_or_create_pipeline_arn",
        lambda recipe, infra, dist: "arn:pipeline:same",
    )

    started = {"called": False}

    class _Client:
        def start_image_pipeline_execution(self, imagePipelineArn):
            started["called"] = True
            return {}

    monkeypatch.setattr(builder, "_client", lambda: _Client())

    result = builder.deploy()

    assert result is builder
    assert started["called"] is False
