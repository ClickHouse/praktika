from dataclasses import dataclass
from typing import Optional, List

from recurcipy.settings import Settings


class Artifact:
    class Type:
        GH = "github"
        PHONY = "phony"

    @dataclass
    class Config:
        """
        @name artifact name
        @type artifact type, see Artifact.Type
        @path file path or glob, e.g. "path/**/[abc]rtifac?/*"
        """

        name: str
        type: str
        path: str

    @classmethod
    def define_artifact(cls, name, type, path):
        return cls.Config(name=name, type=type, path=path)

    @classmethod
    def define_gh_artifact(cls, name, path):
        return cls.define_artifact(name=name, type=cls.Type.GH, path=path)
