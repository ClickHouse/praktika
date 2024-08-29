import dataclasses
import glob
import hashlib
import os
from hashlib import md5
from typing import List

from praktika import Job
from praktika.docker import Docker
from praktika.settings import Settings


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

    def calc_job_digest(self, job_config: Job.Config):
        """

        :param job_config:
        :return:
        """
        config = job_config.digest_config
        if not config:
            return "f" * Settings.CACHE_DIGEST_LEN

        cache_key = self._hash_digest_config(config)

        if cache_key in self.digest_cache:
            return self.digest_cache[cache_key]

        # Get the list of included files
        included_files_ = set()
        for path in config.include_paths:
            included_files_.update(self._path_to_files_recursively(path))

        # Filter out excluded files
        excluded_files = set()
        for path in config.exclude_paths:
            res = self._path_to_files_recursively(path)
            if not res:
                print(
                    f"WARNING: DigestConfig.exclude_files path [{path}] filtered 0 files"
                )
            else:
                excluded_files.update(res)

        included_files = [f for f in included_files_ if f not in excluded_files]

        print(f"calc digest: hash_key [{cache_key}], include [{included_files}] files")
        # Sort files to ensure consistent hash calculation
        included_files.sort()

        # Calculate MD5 hash
        res = ""
        if not included_files:
            res = "f" * Settings.CACHE_DIGEST_LEN
            print(f"NOTE: empty digest config [{config}] - return dummy digest")
        else:
            hash_md5 = hashlib.md5()
            for file_path in included_files:
                res = self._calc_file_digest(file_path, hash_md5)
        assert res
        self.digest_cache[cache_key] = res
        return res

    def calc_docker_digest(
        self,
        docker_config: Docker.Config,
        dependency_configs: List[Docker.Config],
        hash_md5=None,
    ):
        """

        :param hash_md5:
        :param dependency_configs: list of Docker.Config(s) that :param docker_config: depends on
        :param docker_config: Docker.Config to calculate digest for
        :return:
        """
        print(f"Calculate digest for docker [{docker_config.name}]")
        paths = self._path_to_files_recursively(docker_config.path)
        if not hash_md5:
            hash_md5 = hashlib.md5()

        dependencies = []
        for dependency_name in docker_config.depend_on:
            for dependency_config in dependency_configs:
                if dependency_config.name == dependency_name:
                    print(
                        f"Add docker [{dependency_config.name}] as dependency for docker [{docker_config.name}] digest calculation"
                    )
                    dependencies.append(dependency_config)

        for dependency in dependencies:
            _ = self.calc_docker_digest(dependency, dependency_configs, hash_md5)

        for path in paths:
            _ = self._calc_file_digest(path, hash_md5=hash_md5)

        return hash_md5.hexdigest()[: Settings.CACHE_DIGEST_LEN]

    @staticmethod
    def _calc_file_digest(file_path, hash_md5):
        # Calculate MD5 hash
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)

        res = hash_md5.hexdigest()[: Settings.CACHE_DIGEST_LEN]
        return res

    @staticmethod
    def _path_to_files_recursively(path):
        res = []
        if os.path.isfile(path):
            res.append(path)
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for file in files:
                    res.append(os.path.join(root, file))
        elif "*" in str(path):
            res.extend(
                [f for f in glob.glob(path, recursive=True) if os.path.isfile(f)]
            )
        else:
            assert False, f"File does not exist or not valid [{path}]"
        return res
