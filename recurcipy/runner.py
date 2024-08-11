import argparse
import sys

from recurcipy import Shell
from recurcipy.mangle import _get_workflows
from recurcipy.environment import Environment


class Runner:

    def pre_run(self, job_name, workflow_name):
        print(f"Pre-run script [{job_name}]")
        envs = {
            "JOB_NAME": job_name
        }
        print(f"Exporting env variables [{envs}]")
        for k, v in envs.items():
            Shell.check(f"echo {k}=\"{v}\" >> $GITHUB_ENV")
        Shell.check("cat $GITHUB_ENV")
        pass

    def run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run script [{job_name}], workflow [{workflow.name}]")

        if not workflow:
            print(f"ERROR: failed to get workflow [{workflow.name}]")
        job = workflow.get_job(job_name)
        assert job
        print(f"Run command [{job.command}]")
        return Shell.run(job.command)

    def post_run(self, job_name, workflow_name):
        print(f"Post-run script [{job_name}]")
        pass


def parse_args():
    parser = argparse.ArgumentParser("RecurCIPY")
    parser.add_argument(
        "--job-name",
        required=True,
        type=str,
    )
    parser.add_argument(
        "--workflow-name",
        required=True,
        type=str,
    )
    parser.add_argument(
        "--pre-run",
        action="store_true",
        help="Runs pre-run step for --job-name",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Runs run step for --job-name",
    )
    parser.add_argument(
        "--post-run",
        action="store_true",
        help="Runs post-run step for --job-name",
    )
    return parser.parse_args(), parser


if __name__ == '__main__':
    args, parser = parse_args()
    res = 0

    if args.pre_run:
        res = Runner().pre_run(args.job_name, args.workflow_name)
    elif args.run:
        res = Runner().run(args.job_name, args.workflow_name)
    elif args.post_run:
        res = Runner().post_run(args.job_name, args.workflow_name)
    else:
        assert False

    sys.exit(res)
