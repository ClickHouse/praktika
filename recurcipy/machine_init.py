import os
import platform
import signal

import requests

from recurcipy import Shell, ContextManager, Environment
from recurcipy.settings import Settings


class Machine:
    @staticmethod
    def get_latest_gh_actions_release():
        url = f"https://api.github.com/repos/actions/runner/releases/latest"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            latest_release = response.json()
            return latest_release["tag_name"].removeprefix("v")
        else:
            print(f"Failed to get the latest release: {response.status_code}")
            return None

    def __init__(self):
        self.os_name = platform.system().lower()
        assert self.os_name == "linux", f"Unsupported OS [{self.os_name}]"
        if platform.machine() == "x86_64":
            self.arch = "x64"
        elif "aarch64" in platform.machine().lower():
            self.arch = "arm64"
        else:
            assert False, f"Unsupported arch [{platform.machine()}]"
        self.gh_token = None
        self.instance_id = None
        self.runner_api_endpoint = None
        self.runner_type = None
        self.labels = []

    def _install_gh_actions_runner(self):
        gh_actions_version = self.get_latest_gh_actions_release()
        assert self.os_name and gh_actions_version and self.arch
        Shell.check(
            f"rm -rf {Settings.GH_ACTIONS_DIRECTORY}", strict=True, verbose=True
        )
        Shell.check(f"mkdir {Settings.GH_ACTIONS_DIRECTORY}", strict=True, verbose=True)
        with ContextManager.cd(Settings.GH_ACTIONS_DIRECTORY):
            Shell.check(
                f"curl -O -L https://github.com/actions/runner/releases/download/v{gh_actions_version}/actions-runner-{self.os_name}-{self.arch}-{gh_actions_version}.tar.gz",
                strict=True,
                verbose=True,
            )
            Shell.check(f"tar xzf *tar.gz", strict=True, verbose=True)
            Shell.check(f"rm -f *tar.gz", strict=True, verbose=True)
            Shell.check(f"sudo ./bin/installdependencies.sh", strict=True, verbose=True)
            Shell.check(
                f"chown -R ubuntu:ubuntu {Settings.GH_ACTIONS_DIRECTORY}",
                strict=True,
                verbose=True,
            )

    def _get_gh_token_from_ssm(self):
        if not self.gh_token:
            self.gh_token = Shell.get_output_or_raise(
                "/usr/local/bin/aws ssm  get-parameter --name github_runner_registration_token --with-decryption --output text --query Parameter.Value"
            )
        return self.gh_token

    def update_instance_info(self):
        self.instance_id = Shell.get_output_or_raise("ec2metadata --instance-id")
        assert self.instance_id
        org = os.getenv("MY_ORG", "")
        assert (
            org
        ), "MY_ORG env variable myst be set to use init script for runner machine"
        self.runner_api_endpoint = f"https://github.com/{org}"
        self.runner_type = Shell.get_output_or_raise(
            f'/usr/local/bin/aws ec2 describe-tags --filters "Name=resource-id,Values={self.instance_id}" --query "Tags[?Key==\'github:runner-type\'].Value" --output text'
        )
        assert self.runner_type
        self.labels = ["self-hosted", self.runner_type]
        return self

    def config_actions(self):
        if not self.instance_id:
            self.update_instance_info()
        if not self.gh_token:
            self._get_gh_token_from_ssm()
        assert (
            self.gh_token
            and self.instance_id
            and self.runner_api_endpoint
            and self.labels
        )
        command = f"sudo -u ubuntu {Settings.GH_ACTIONS_DIRECTORY}/config.sh --token {self.gh_token}\
            --url {self.runner_api_endpoint} --ephemeral --unattended --replace\
            --runnergroup Default --labels {','.join(self.labels)} --work wd --name {self.instance_id}"
        Shell.check(command, strict=True, verbose=True)

    def unconfig_actions(self):
        if not self.gh_token:
            self._get_gh_token_from_ssm()
        command = f"sudo -u ubuntu {Settings.GH_ACTIONS_DIRECTORY}/config.sh remove --token {self.gh_token}"
        Shell.check(command, strict=True, verbose=True)

    def run_actions(self):
        if not self.gh_token:
            self._get_gh_token_from_ssm()
        command = f"sudo -u ubuntu {Settings.GH_ACTIONS_DIRECTORY}/run.sh"
        Shell.check(command, strict=True, verbose=True)

    def self_terminate(self):
        if not self.instance_id:
            self.update_instance_info()
        assert self.instance_id
        Shell.check(
            f"aws autoscaling terminate-instance-in-auto-scaling-group --instance-id {self.instance_id}",
            verbose=True,
        )


def handle_signal(signum, _frame):
    print(f"FATAL: Received signal {signum}")
    raise RuntimeError(f"killed by signal {signum}")


def run():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    m = None
    try:
        m = Machine().update_instance_info()
        m.config_actions()
        m.run_actions()
    except Exception as e:
        print(f"FATAL: Exception [{e}] - terminate instance")
        Machine().unconfig_actions()
        if not Environment.LOCAL_EXECUTION:
            if m is not None:
                m.self_terminate()
            else:
                print("ERROR: failed to initialize aws env - terminate via os")
                os.system("sudo shutdown now")
        else:
            print("NOTE: Local execution - machin won't ne terminated")


if __name__ == "__main__":
    run()
