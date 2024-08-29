import dataclasses
from typing import List

from praktika.utils import Shell


class Docker:

    @dataclasses.dataclass
    class Config:
        name: str
        path: str
        depend_on: List[str]
        amd64: bool
        arm64: bool

    @classmethod
    def build(cls, config: "Docker.Config", log_file, digests, add_latest):
        platforms = []
        if config.arm64:
            platforms.append("linux/arm64")
        if config.amd64:
            platforms.append("linux/amd64")

        tags_substr = f" -t {config.name}:{digests[config.name]}"
        if add_latest:
            tags_substr = f" -t {config.name}:latest"

        from_tag = ""
        if config.depend_on:
            assert (
                len(config.depend_on) == 1
            ), f"Only one dependency in depend_on is currently supported, docker [{config}]"
            from_tag = f" --build-arg FROM_TAG={digests[config.depend_on[0]]}"

        command = f"docker buildx build --platform {','.join(platforms)} {tags_substr} {from_tag} --push {config.path}"
        return Shell.run(command, log_file=log_file, verbose=True, strict=True)

    @classmethod
    def sort_in_build_order(cls, dockers: List["Docker.Config"]):
        ready_names = []
        i = 0
        while i < len(dockers):
            docker = dockers[i]
            if not docker.depend_on or all(
                dep in ready_names for dep in docker.depend_on
            ):
                ready_names.append(docker.name)
                i += 1
            else:
                dockers.append(dockers.pop(i))
        return dockers

    @classmethod
    def login(cls, user_name, user_password):
        print("Docker: log in to dockerhub")
        return Shell.check(
            f"docker login --username '{user_name}' --password-stdin",
            strict=True,
            stdin_str=user_password,
            encoding="utf-8",
            verbose=True,
        )
