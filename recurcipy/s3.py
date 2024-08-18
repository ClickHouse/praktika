from pathlib import Path

from recurcipy import Shell
from recurcipy.settings import Settings


class S3Utils:
    @classmethod
    def copy_artifact_from_s3(cls, sha, name):
        return Shell.check(
            f"aws s3 cp s3://{Settings.S3_ARTIFACT_PATH}/{sha}/{Path(name).name} {Settings.INPUT_DIR}/{Path(name).name}",
            verbose=True,
        )

    @classmethod
    def copy_artifact_to_s3(cls, sha, path):
        assert Path(path), f"Artifact [{path}] doe not exist"
        assert Path(
            path
        ).is_file(), (
            f"Artifact [{path}] is not file. Only files are supported with S3 storage"
        )
        return Shell.check(
            f"aws s3 cp {path} s3://{Settings.S3_ARTIFACT_PATH}/{sha}/{Path(path).name}",
            verbose=True,
        )
