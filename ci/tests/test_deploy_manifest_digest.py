from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.cloud import CloudInfrastructure

_digest = CloudInfrastructure.Config._component_digest


def _asg(**overrides):
    base = dict(
        name="praktika-workflow-orchestrator",
        region="eu-north-1",
        vpc_name="praktika-ci",
        subnet_ids=["subnet-1"],
        min_size=0,
        max_size=1,
        desired_capacity=1,
        launch_template_name="praktika-workflow-orchestrator-lt",
        launch_template_version="$Default",
        praktika_resource_tag="workflow_orchestrator",
    )
    base.update(overrides)
    return AutoScalingGroup.Config(**base)


def test_digest_is_stable_for_identical_config():
    assert _digest(_asg()) == _digest(_asg())


def test_digest_changes_when_a_real_field_changes():
    # A config change the manifest must NOT mask as "unchanged".
    assert _digest(_asg(max_size=1)) != _digest(_asg(max_size=5))


def test_digest_ignores_runtime_region():
    # region is set from settings at deploy time, folded into the schema
    # fingerprint instead — it must not perturb the per-component digest.
    assert _digest(_asg(region="eu-north-1")) == _digest(_asg(region="us-east-1"))


def test_digest_ignores_mutated_ext():
    # ext holds runtime state (arns, cached ids, fetched sizes); mutating it
    # after a fetch must not flip a component from skip to redeploy.
    cfg = _asg()
    before = _digest(cfg)
    cfg.ext.update({"min_size": 99, "launch_template": {"version": "42"}})
    assert _digest(cfg) == before


def test_digest_survives_reference_cycle():
    # A launch template references its image builder, which lists the template
    # back — a cycle. The digest must terminate with a stable value, never a
    # RecursionError (which the deploy would silently swallow into "never skip").
    from praktika.infrastructure.image_builder import ImageBuilder
    from praktika.infrastructure.launch_template import LaunchTemplate

    lt = LaunchTemplate.Config(name="praktika-cyc-lt", region="eu-north-1")
    ib = ImageBuilder.Config(name="praktika-cyc-ib", launch_templates=[lt])
    lt.image_builder = ib  # close the cycle

    first = _digest(lt)
    assert first is not None
    assert first == _digest(lt)  # stable


def test_lambda_source_fingerprint_tracks_file_content(tmp_path):
    # A code change with no config change must still bust the digest, so a
    # redeployed Lambda is never skipped on stale code.
    from praktika.infrastructure.lambda_function import Lambda

    src = tmp_path / "handler.py"
    src.write_text("def handler(e, c): return 1\n")
    cfg = Lambda.Config(name="praktika-fn", handler="handler.handler", path=str(src))

    before = _digest(cfg, CloudInfrastructure.Config._lambda_source_fingerprint(cfg))
    src.write_text("def handler(e, c): return 2\n")  # same config, new code
    after = _digest(cfg, CloudInfrastructure.Config._lambda_source_fingerprint(cfg))
    assert before != after


def test_launch_template_references_image_builder_by_name_not_content():
    # An LT digests its image builder by name only (the builder has its own
    # manifest entry); the builder's unrelated internals must not perturb the
    # LT digest, and the LT<->builder cycle must not recurse.
    from praktika.infrastructure.image_builder import ImageBuilder
    from praktika.infrastructure.launch_template import LaunchTemplate

    def _make(builder_instance_type):
        lt = LaunchTemplate.Config(name="praktika-lt", region="eu-north-1")
        ib = ImageBuilder.Config(
            name="praktika-ib",
            launch_templates=[lt],
            instance_types=[builder_instance_type],
        )
        lt.image_builder = ib
        return lt

    # Different builder internals, same builder name -> identical LT digest.
    assert _digest(_make("c7g.large")) == _digest(_make("c7g.xlarge"))


_orphan = CloudInfrastructure.Config._orphan_delete_config


def test_orphan_reconstructs_deletable_kinds_by_name():
    # Every deletable kind rebuilds from kind+name+region alone (delete() reads
    # only those), so orphan teardown works without the original config object.
    for kind in CloudInfrastructure.Config._ORPHAN_DELETE_ORDER:
        cfg = _orphan(kind, "praktika-gone", "eu-north-1")
        assert cfg is not None, kind
        assert cfg.name == "praktika-gone"
        assert cfg.region == "eu-north-1"
        assert hasattr(cfg, "delete")


def test_orphan_returns_none_for_kinds_without_delete():
    # These have no delete(); they must fall through to the manual warning,
    # never a bogus reconstructed-and-deleted call.
    for kind in ("sqs_queue", "ec2_instance", "dedicated_host"):
        assert _orphan(kind, "praktika-gone", "eu-north-1") is None
