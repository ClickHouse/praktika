import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union, Optional


class MetaClasses:
    class WithIter(type):
        def __iter__(cls):
            return (v for k, v in cls.__dict__.items() if not k.startswith("_"))


class ContextManager:

    @staticmethod
    @contextmanager
    def cd(to: Optional[Union[Path, str]] = None) -> Iterator[None]:
        """
        changes current workin directory to @path or `git root` if @path is None
        :param to:
        :return:
        """
        if not to:
            to = Shell.get_output_or_raise("git rev-parse --show-toplevel")
        oldpwd = os.getcwd()
        os.chdir(to)
        try:
            yield
        finally:
            os.chdir(oldpwd)


class Shell:
    @classmethod
    def get_output_or_raise(cls, command):
        return cls.get_output(command, strict=True).strip()

    @classmethod
    def get_output(cls, command, strict=False):
        res = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=strict,
        )
        return res.stdout

    @classmethod
    def check(
            cls,
            command,
            strict=False,
            verbose=False,
            dry_run=False,
            stdin_str=None,
            **kwargs,
    ):
        if dry_run:
            print(f"Dry-ryn. Would run command [{command}]")
            return True
        if verbose:
            print(f"Run command [{command}]")
        proc = subprocess.Popen(
            command,
            shell=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_str else None,
            universal_newlines=True,
            start_new_session=True,
            bufsize=1,
            errors="backslashreplace",
            **kwargs,
        )
        if stdin_str:
            proc.communicate(input=stdin_str)
        elif proc.stdout:
            for line in proc.stdout:
                sys.stdout.write(line)
        proc.wait()
        if strict:
            assert proc.returncode == 0
        return proc.returncode == 0

    @classmethod
    def run(
            cls,
            command,
            strict=False,
            stdin_str=None,
            **kwargs,
    ):
        proc = subprocess.Popen(
            command,
            shell=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_str else None,
            universal_newlines=True,
            start_new_session=True,
            bufsize=1,
            errors="backslashreplace",
            **kwargs,
        )
        if stdin_str:
            proc.communicate(input=stdin_str)
        elif proc.stdout:
            for line in proc.stdout:
                sys.stdout.write(line)
        proc.wait()
        if strict:
            assert proc.returncode == 0
        return proc.returncode


class Utils:
    @staticmethod
    def get_failed_tests_number(description: str) -> Optional[int]:
        description = description.lower()

        pattern = r"fail:\s*(\d+)\s*(?=,|$)"
        match = re.search(pattern, description)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def is_killed_with_oom():
        if Shell.check(
                "sudo dmesg -T | grep -q -e 'Out of memory: Killed process' -e 'oom_reaper: reaped process' -e 'oom-kill:constraint=CONSTRAINT_NONE'"
        ):
            return True
        return False

    @staticmethod
    def clear_dmesg():
        Shell.check("sudo dmesg --clear", verbose=True)

    @staticmethod
    def is_hex(s):
        try:
            int(s, 16)
            return True
        except ValueError:
            return False

    @staticmethod
    def normalize_string(string: str) -> str:
        res = string.lower()
        for r in (
                (" ", "_"),
                ("(", "_"),
                (")", "_"),
                (",", "_"),
                ("/", "_"),
                ("-", "_"),
        ):
            res = res.replace(*r)
        return res
