import json
import math
import os
import time

import boto3

# S3 key prefix (under the project artifact bucket) where this autoscaler
# publishes each ASG's current target AMI. Must match the consumer side in
# bootstrap/src/praktika_controller/common.py (AMI_TARGET_S3_PREFIX). Runners
# read these objects to decide AMI-drift self-recycle instead of describing the
# ASG themselves (which caused account-wide DescribeAutoScalingGroups
# throttling when thousands crossed the idle boundary at once).
AMI_TARGET_S3_PREFIX = "ami-targets"


def _asg_launch_template_spec(group):
    """The LaunchTemplateSpecification an ASG currently launches with, whether it
    uses a plain LaunchTemplate or a MixedInstancesPolicy. {} if neither is set."""
    spec = group.get("LaunchTemplate")
    if spec:
        return spec
    return (
        group.get("MixedInstancesPolicy", {})
        .get("LaunchTemplate", {})
        .get("LaunchTemplateSpecification", {})
    )


def _resolve_target_ami(ec2, group):
    """Resolve the AMI the ASG's current launch-template version would launch.
    Returns "" if the ASG has no launch template or the version has no ImageId."""
    spec = _asg_launch_template_spec(group)
    lt_id = spec.get("LaunchTemplateId")
    lt_name = spec.get("LaunchTemplateName")
    if not lt_id and not lt_name:
        return ""
    kwargs = {"Versions": [spec.get("Version") or "$Default"]}
    if lt_id:
        kwargs["LaunchTemplateId"] = lt_id
    else:
        kwargs["LaunchTemplateName"] = lt_name
    versions = ec2.describe_launch_template_versions(**kwargs)["LaunchTemplateVersions"]
    if not versions:
        return ""
    return versions[0].get("LaunchTemplateData", {}).get("ImageId", "")


def _publish_target_ami(s3, bucket, asg_name, target_ami):
    """Publish the ASG's target AMI so its runners can read it from S3 instead of
    describing the ASG. Best-effort: never let a publish failure affect scaling."""
    if not (bucket and asg_name and target_ami):
        return
    s3.put_object(
        Bucket=bucket,
        Key=f"{AMI_TARGET_S3_PREFIX}/{asg_name}.json",
        Body=json.dumps(
            {"target_ami": target_ami, "updated_at": int(time.time())}
        ).encode("utf-8"),
        ContentType="application/json",
    )


def _load_pool_configs():
    raw = os.environ.get("POOLS_CONFIG_JSON", "[]").strip()
    if not raw:
        return []
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("POOLS_CONFIG_JSON must contain a JSON array")
    return payload


def _calculate_desired_capacity(
    *,
    current_desired: int,
    max_size: int,
    visible_messages: int,
    in_flight_messages: int,
    capacity_reserve: int = 0,
) -> int:
    # Scheduled autoscaling is scale-up only. Scale-in is owned by the VMs
    # themselves after they observe sustained queue idleness from inside the
    # worker/orchestrator loop. Never return a lower desired capacity here,
    # even if backlog is currently zero.
    backlog = max(0, int(visible_messages)) + max(0, int(in_flight_messages))
    capacity_reserve = max(0, int(capacity_reserve))
    current_work_capacity = max(0, int(current_desired) - capacity_reserve)
    required_work_capacity = math.ceil(backlog / 1) if backlog else 0
    target_work_capacity = max(current_work_capacity, required_work_capacity)
    if target_work_capacity <= current_work_capacity:
        return min(max_size, max(current_desired, capacity_reserve))

    # Grow toward the target by max(30% of the OUTSTANDING gap, 1) per
    # invocation (the schedule fires once a minute). The gap is backlog not yet
    # covered by current_desired, which already includes instances we requested
    # on earlier runs but that are still booting. Stepping off the gap (rather
    # than the raw backlog) means each minute decelerates as pending capacity
    # ramps up, instead of slamming in another big batch for a backlog that is
    # already being provisioned for and just hasn't been picked up yet.
    gap = target_work_capacity - current_work_capacity
    step = max(1, math.ceil(gap * 0.3))
    new_work_capacity = min(target_work_capacity, current_work_capacity + step)
    target_capacity = min(max_size, capacity_reserve + new_work_capacity)
    if target_capacity <= current_desired:
        return current_desired
    return max(current_desired, target_capacity)


def lambda_handler(event, context):
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    sqs = boto3.client("sqs", region_name=region)
    autoscaling = boto3.client("autoscaling", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    ami_target_bucket = (os.environ.get("AMI_TARGET_BUCKET", "") or "").strip()

    results = []
    skipped = []
    for pool in _load_pool_configs():
        pool_name = str(pool["name"])
        queue_name = str(pool.get("queue_name") or pool_name)
        asg_name = str(pool.get("asg_name") or pool_name)
        capacity_reserve = max(0, int(pool.get("capacity_reserve") or 0))

        # A single pool whose SQS queue or ASG is missing (e.g. not deployed
        # yet, renamed, or not covered by this role's IAM scope) must not abort
        # the whole run: the remaining pools still need to be scaled. Log the
        # reason and skip just this pool.
        try:
            queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
            queue_attrs = sqs.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            visible_messages = int(queue_attrs.get("ApproximateNumberOfMessages", "0"))
            in_flight_messages = int(
                queue_attrs.get("ApproximateNumberOfMessagesNotVisible", "0")
            )

            group = autoscaling.describe_auto_scaling_groups(
                AutoScalingGroupNames=[asg_name]
            )["AutoScalingGroups"]
            if not group:
                raise RuntimeError(f"Auto Scaling Group '{asg_name}' was not found")
            group = group[0]

            # Publish this ASG's target AMI so its runners read it from S3 at the
            # idle boundary instead of each describing the ASG themselves. Isolated
            # try: a publish failure must never skip this pool's scale-up.
            if ami_target_bucket:
                try:
                    _publish_target_ami(
                        s3, ami_target_bucket, asg_name, _resolve_target_ami(ec2, group)
                    )
                except Exception as ex:  # noqa: BLE001
                    print(
                        f"[WARN] could not publish target AMI for asg='{asg_name}': "
                        f"{type(ex).__name__}: {ex}"
                    )

            current_desired = int(group["DesiredCapacity"])
            max_size = int(group["MaxSize"])

            proposed_desired = _calculate_desired_capacity(
                current_desired=current_desired,
                max_size=max_size,
                visible_messages=visible_messages,
                in_flight_messages=in_flight_messages,
                capacity_reserve=capacity_reserve,
            )
            # Defensive clamp: even if future _calculate_desired_capacity changes,
            # this autoscaler must never scale a pool down. VM self-termination is
            # the only supported scale-in path.
            new_desired = max(current_desired, proposed_desired)
            scaled = new_desired > current_desired
            if scaled:
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=asg_name,
                    DesiredCapacity=new_desired,
                )
        except Exception as ex:
            reason = f"{type(ex).__name__}: {ex}"
            print(
                f"[WARN] skipping pool '{pool_name}' "
                f"(queue='{queue_name}', asg='{asg_name}'): {reason}"
            )
            skipped.append(
                {
                    "pool_name": pool_name,
                    "asg_name": asg_name,
                    "queue_name": queue_name,
                    "error": reason,
                }
            )
            continue

        results.append(
            {
                "pool_name": pool_name,
                "asg_name": asg_name,
                "queue_name": queue_name,
                "visible_messages": visible_messages,
                "in_flight_messages": in_flight_messages,
                "current_desired": current_desired,
                "capacity_reserve": capacity_reserve,
                "new_desired": new_desired,
                "scaled": scaled,
            }
        )

    return {
        "region": region,
        "pool_count": len(results),
        "results": results,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }
