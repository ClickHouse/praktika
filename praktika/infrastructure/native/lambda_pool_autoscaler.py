import json
import math
import os

import boto3


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
) -> int:
    backlog = max(0, int(visible_messages)) + max(0, int(in_flight_messages))
    required_capacity = math.ceil(backlog / 1) if backlog else 0
    target_capacity = min(max_size, max(current_desired, required_capacity))
    if target_capacity <= current_desired:
        return current_desired
    return min(target_capacity, current_desired + 1)


def lambda_handler(event, context):
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    sqs = boto3.client("sqs", region_name=region)
    autoscaling = boto3.client("autoscaling", region_name=region)

    results = []
    for pool in _load_pool_configs():
        pool_name = str(pool["name"])
        queue_name = str(pool.get("queue_name") or f"praktika-{pool_name}")
        asg_name = str(pool.get("asg_name") or f"praktika-{pool_name}")

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
        current_desired = int(group["DesiredCapacity"])
        max_size = int(group["MaxSize"])

        new_desired = _calculate_desired_capacity(
            current_desired=current_desired,
            max_size=max_size,
            visible_messages=visible_messages,
            in_flight_messages=in_flight_messages,
        )
        scaled = new_desired > current_desired
        if scaled:
            autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                DesiredCapacity=new_desired,
            )

        results.append(
            {
                "pool_name": pool_name,
                "asg_name": asg_name,
                "queue_name": queue_name,
                "visible_messages": visible_messages,
                "in_flight_messages": in_flight_messages,
                "current_desired": current_desired,
                "new_desired": new_desired,
                "scaled": scaled,
            }
        )

    return {
        "region": region,
        "pool_count": len(results),
        "results": results,
    }
