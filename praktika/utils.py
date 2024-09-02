import dataclasses
import inspect
import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Union, Optional, TypeVar, Type, Dict, Any

from praktika._settings import _Settings

T = TypeVar("T", bound="Serializable")


class MetaClasses:
    class WithIter(type):
        def __iter__(cls):
            return (v for k, v in cls.__dict__.items() if not k.startswith("_"))

    class FormatPrint:
        @classmethod
        def format_print(cls, message):
            calling_function_name = inspect.stack()[1].function
            print(f"{cls.__class__.__name__}::{calling_function_name}: {message}")

    @dataclasses.dataclass
    class Serializable(ABC):
        @classmethod
        def from_dict(cls: Type[T], obj: Dict[str, Any]) -> T:
            return cls(**obj)

        @classmethod
        def from_fs(cls: Type[T], name) -> T:
            with open(cls.file_name_static(name), "r", encoding="utf8") as f:
                try:
                    return cls.from_dict(json.load(f))
                except json.decoder.JSONDecodeError as ex:
                    print(f"ERROR: failed to parse json, ex [{ex}]")
                    print(f"JSON content [{cls.file_name_static(name)}]:\n {f.read()}")
                    raise ex

        @classmethod
        @abstractmethod
        def file_name_static(cls, name):
            pass

        def file_name(self):
            return self.file_name_static(self.name)

        def dump(self):
            with open(self.file_name(), "w", encoding="utf8") as f:
                json.dump(dataclasses.asdict(self), f, indent=4)
            return self

        def to_json(self, pretty=False):
            return json.dumps(dataclasses.asdict(self), indent=4 if pretty else None)


class ContextManager:
    @staticmethod
    @contextmanager
    def cd(to: Optional[Union[Path, str]] = None) -> Iterator[None]:
        """
        changes current working directory to @path or `git root` if @path is None
        :param to:
        :return:
        """
        if not to:
            try:
                to = Shell.get_output_or_raise("git rev-parse --show-toplevel")
            except:
                pass
            if not to:
                if Path(_Settings.DOCKER_WD).is_dir():
                    to = _Settings.DOCKER_WD
            if not to:
                assert False, "FIX IT"
            assert to
        old_pwd = os.getcwd()
        os.chdir(to)
        try:
            yield
        finally:
            os.chdir(old_pwd)


class Shell:
    @classmethod
    def get_output_or_raise(cls, command):
        return cls.get_output(command).strip()

    @classmethod
    def get_output(cls, command, strict=False):
        res = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.stderr:
            print(f"WARNING: stderr: {res.stderr.strip()}")
        if strict and res.returncode != 0:
            raise RuntimeError(f"command failed with {res.returncode}")
        return res.stdout.strip()

    @classmethod
    def get_output_and_code(cls, command, strict=False):
        res = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=strict,
        )
        if res.stderr:
            print(f"WARNING: stderr: {res.stderr.strip()}")
        return res.stdout.strip(), res.returncode

    @classmethod
    def check(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=False,
        dry_run=False,
        stdin_str=None,
        **kwargs,
    ):
        return (
            cls.run(command, log_file, strict, verbose, dry_run, stdin_str, **kwargs)
            == 0
        )

    @classmethod
    def run(
        cls,
        command,
        log_file=None,
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

        log_file = log_file or "/dev/null"
        with open(log_file, "w") as log_fp:
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
                    log_fp.write(line)
            proc.wait()
        if strict:
            assert proc.returncode == 0
        return proc.returncode

    @classmethod
    def run_async(
        cls,
        command,
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
        return proc


class Utils:
    @staticmethod
    def timestamp():
        return datetime.utcnow().timestamp()

    @staticmethod
    def timestamp_to_str(timestamp):
        return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

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
            (":", ""),
        ):
            res = res.replace(*r)
        return res

    class Stopwatch:
        def __init__(self):
            self.start_time = datetime.utcnow().timestamp()

        @property
        def duration(self) -> float:
            return datetime.utcnow().timestamp() - self.start_time


if __name__ == "__main__":

    @dataclasses.dataclass
    class Test(MetaClasses.Serializable):
        name: str

        @staticmethod
        def file_name_static(name):
            return f"/tmp/{Utils.normalize_string(name)}.json"

    Test(name="dsada").dump()
    t = Test.from_fs("dsada")
    print(t)
