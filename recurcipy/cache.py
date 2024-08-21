import dataclasses
import fnmatch
import glob
import hashlib
import json
import os
from hashlib import md5
from pathlib import Path

from recurcipy import Job, Workflow, Artifact
from recurcipy.s3 import S3Utils
from recurcipy.settings import Settings, Environment
from recurcipy.utils import Utils


class Cache:
    @dataclasses.dataclass
    class CacheRecord:
        class Type:
            SUCCESS = "success"

        type: str
        sha: str
        pr_number: int
        branch: str

        def dump(self, path):
            with open(path, "w", encoding="utf8") as f:
                json.dump(dataclasses.asdict(self), f)

        @classmethod
        def from_fs(cls, path):
            with open(path, "r", encoding="utf8") as f:
                return Cache.CacheRecord(**json.load(f))

    def __init__(self):
        self.digest = self.Digest()
        self.success = {}  # type Dict[str, Any]

    class Digest:
        def __init__(self):
            self.digest_cache = {}

        @staticmethod
        def _hash_digest_config(digest_config: Job.CacheDigestConfig) -> str:
            data_dict = dataclasses.asdict(digest_config)
            hash_obj = md5()
            hash_obj.update(str(data_dict).encode())
            hash_string = hash_obj.hexdigest()
            return hash_string

        def calc_digest(self, config):
            """
            Calculate the MD5 hash of files based on the CacheDigestConfig.

            Args:
                config (CacheDigestConfig): Configuration for included and excluded paths.

            Returns:
                str: The MD5 hash of the included files.
            """
            if not config or not config.include_paths:
                return "f" * Settings.CACHE_DIGEST_LEN

            cache_key = self._hash_digest_config(config)

            if cache_key in self.digest_cache:
                return self.digest_cache[cache_key]

            # Get the list of included files
            included_files = []
            if config.include_paths:
                for path in config.include_paths:
                    if os.path.isfile(path):
                        included_files.append(path)
                    elif os.path.isdir(path):
                        for root, dirs, files in os.walk(path):
                            for file in files:
                                included_files.append(os.path.join(root, file))
                    elif "*" in str(path):
                        included_files.extend(
                            [
                                f
                                for f in glob.glob(path, recursive=True)
                                if os.path.isfile(f)
                            ]
                        )
                    else:
                        assert False, f"File does not exist or not valid [{path}]"

            # Filter out excluded files
            if config.exclude_paths:
                excluded_files = set()
                for path in config.exclude_paths:
                    if os.path.isfile(path):
                        excluded_files.add(path)
                    elif os.path.isdir(path):
                        for root, dirs, files in os.walk(path):
                            for file in files:
                                excluded_files.add(os.path.join(root, file))
                    elif "*" in str(path):
                        matching_files = [
                            file
                            for file in included_files
                            if fnmatch.fnmatch(file, path)
                        ]
                        excluded_files.update(matching_files)
                    else:
                        print(
                            f"WARNING: digest exclude file does not exist or not valid [{path}]"
                        )

                included_files = [f for f in included_files if f not in excluded_files]

            print(
                f"calc digest: hash_key [{cache_key}], include [{included_files}] files"
            )
            # Sort files to ensure consistent hash calculation
            included_files.sort()

            # Calculate MD5 hash
            hash_md5 = hashlib.md5()
            for file_path in included_files:
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(4096), b""):
                        hash_md5.update(chunk)

            res = hash_md5.hexdigest()[: Settings.CACHE_DIGEST_LEN]
            self.digest_cache[cache_key] = res
            return res

    @classmethod
    def push_success_record(cls, job_name, job_digest, sha):
        type_ = Cache.CacheRecord.Type.SUCCESS
        record = Cache.CacheRecord(
            type=type_,
            sha=sha,
            pr_number=Environment.EventInfo.PR_NUMBER,
            branch=Environment.BRANCH,
        )
        assert (
            Settings.CACHE_S3_PATH
        ), f"Setting CACHE_S3_PATH must be defined with enabled CI Cache"
        record_path = f"{Settings.CACHE_S3_PATH}/v{Settings.CACHE_VERSION}/{Utils.normalize_string(job_name)}/{job_digest}"
        record_file = Path(Settings.TEMP_DIR) / type_
        record.dump(record_file)
        S3Utils.copy_file_to_s3(s3_path=record_path, local_path=record_file)
        record_file.unlink()

    def fetch_success(self, job_name, job_digest):
        type_ = Cache.CacheRecord.Type.SUCCESS
        assert (
            Settings.CACHE_S3_PATH
        ), f"Setting CACHE_S3_PATH must be defined with enabled CI Cache"
        record_path = f"{Settings.CACHE_S3_PATH}/v{Settings.CACHE_VERSION}/{Utils.normalize_string(job_name)}/{job_digest}/{type_}"
        record_file_local_dir = (
            f"{Settings.CACHE_LOCAL_PATH}/{Utils.normalize_string(job_name)}/"
        )
        Path(record_file_local_dir).mkdir(parents=True, exist_ok=True)
        res = S3Utils.copy_file_from_s3(
            s3_path=record_path, local_path=record_file_local_dir
        )
        if res:
            print(f"Cache record found, job [{job_name}], digest [{job_digest}]")
            self.success[job_name] = True
            return Cache.CacheRecord.from_fs(Path(record_file_local_dir) / type_)
        return None


if __name__ == "__main__":
    # test
    c = Cache()
    workflow = Workflow.Config(
        name="TEST",
        event=Workflow.Event.PULL_REQUEST,
        jobs=[
            Job.Config(
                name="JobA",
                runs_on=["some"],
                command="python -m unittest ./ci/tests/example_1/test_example_produce_artifact.py",
                provides=["greet"],
                job_requirements=Job.Requirements(
                    python_requirements_txt="./requirements.txt"
                ),
                cache_digest=Job.CacheDigestConfig(
                    # example: use glob to include files
                    include_paths=["./ci/tests/example_1/test_example_consume*.py"],
                ),
            ),
            Job.Config(
                name="JobB",
                runs_on=["some"],
                command="python -m unittest ./ci/tests/example_1/test_example_consume_artifact.py",
                requires=["greet"],
                job_requirements=Job.Requirements(
                    python_requirements_txt="./requirements.txt"
                ),
                cache_digest=Job.CacheDigestConfig(
                    # example: use dir to include files recursively
                    include_paths=["./ci/tests/example_1"],
                    # example: use glob to exclude files from digest
                    exclude_paths=[
                        "./ci/tests/example_1/test_example_consume*",
                        "./**/*.pyc",
                    ],
                ),
            ),
        ],
        artifacts=[Artifact.Config(type="s3", name="greet", path="hello")],
        enable_cache=True,
    )
    for job in workflow.jobs:
        print(c.digest.calc_digest(job.cache_digest))
