#!/usr/bin/env bash
# Fetch the log for a specific job from the runner's systemd journal.
# Saves journal to S3 to bypass the 48 KB SSM output limit.
#
# Usage:
#   ./tmp/fetch_job_log.sh -j "Config Workflow"          # most recent run
#   ./tmp/fetch_job_log.sh -j "Config Workflow" -n 2     # second-most-recent
#   ./tmp/fetch_job_log.sh -j "Config Workflow" -i i-xxx # specific runner
#   ./tmp/fetch_job_log.sh --list                        # list all job names
set -euo pipefail

INSTANCE_ID=""
JOB_NAME=""
RUN_INDEX=1
LIST_ONLY=false
REGION=us-east-1
S3_BUCKET=clickhouse-test-reports-private
S3_KEY="praktika/job-runner/tmp-journal-$$.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--instance) INSTANCE_ID="$2"; shift 2 ;;
    -j|--job)      JOB_NAME="$2";    shift 2 ;;
    -n)            RUN_INDEX="$2";   shift 2 ;;
    --list)        LIST_ONLY=true;   shift   ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$INSTANCE_ID" ]; then
  INSTANCE_ID=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:praktika_rn,Values=praktika-arm-2xsmall" \
              "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null)
fi
if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
  echo "No running arm-2xsmall runner found" >&2; exit 1
fi

echo "Fetching full journal from $INSTANCE_ID via S3 ..." >&2

CMD_ID=$(aws ssm send-command --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"journalctl -u ci-runner --no-pager | aws s3 cp - s3://$S3_BUCKET/$S3_KEY --region $REGION\"]" \
  --query 'Command.CommandId' --output text)

until STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null) \
  && [ "$STATUS" = "Success" -o "$STATUS" = "Failed" ]; do sleep 2; done

JOURNAL=$(aws s3 cp "s3://$S3_BUCKET/$S3_KEY" - 2>/dev/null)
aws s3 rm "s3://$S3_BUCKET/$S3_KEY" >/dev/null 2>&1 || true

if $LIST_ONLY; then
  echo "$JOURNAL" | grep "Processing task: job_task job=" | \
    sed "s/.*job='\([^']*\)'.*/\1/" | sort -u
  exit 0
fi

if [ -z "$JOB_NAME" ]; then
  echo "Specify a job with -j <name>, or use --list to see available jobs" >&2
  exit 1
fi

echo "$JOURNAL" | python3 -c "
import sys, re
job_name  = sys.argv[1]
run_index = int(sys.argv[2])
lines = sys.stdin.read().splitlines()
start_pat = re.compile(r\"Processing task: job_task job='\" + re.escape(job_name) + r\"'\")
end_pat   = re.compile(r'DONE: message deleted|ERROR processing message')
starts = [i for i, l in enumerate(lines) if start_pat.search(l)]
if not starts:
    print(f\"No runs of '{job_name}' found in journal\", file=sys.stderr); sys.exit(1)
if run_index > len(starts):
    print(f\"Only {len(starts)} run(s) found, requested index {run_index}\", file=sys.stderr); sys.exit(1)
start = starts[-run_index]
end = len(lines)
for i in range(start + 1, len(lines)):
    if end_pat.search(lines[i]):
        end = i + 1; break
for line in lines[start:end]:
    print(line)
" "$JOB_NAME" "$RUN_INDEX"
