from pathlib import Path

from praktika.utils import Shell
from praktika.settings import Settings


class S3:
    @classmethod
    def get_prefix(cls, pr_number, branch, sha):
        prefix = ""
        if pr_number > 0:
            prefix += f"{pr_number}"
        else:
            prefix += f"{branch}"
        if sha:
            prefix += f"/{sha}"
        return prefix

    @classmethod
    def copy_artifact_from_s3(cls, pr_number, branch, sha, name):
        assert sha, "Invalid input"
        return Shell.check(
            f"aws s3 cp s3://{Settings.S3_ARTIFACT_PATH}/{cls.get_prefix(pr_number, branch, sha)}/{Path(name).name} {Settings.INPUT_DIR}/{Path(name).name}",
            verbose=True,
        )

    @classmethod
    def copy_artifact_to_s3(cls, pr_number, branch, sha, path):
        assert Path(path), f"Artifact [{path}] does not exist"
        assert Path(
            path
        ).is_file(), (
            f"Artifact [{path}] is not file. Only files are supported with S3 storage"
        )
        return Shell.check(
            f"aws s3 cp {path} s3://{Settings.S3_ARTIFACT_PATH}/{cls.get_prefix(pr_number, branch, sha)}/{Path(path).name}",
            verbose=True,
        )

    @classmethod
    def copy_file_to_s3(cls, s3_path, local_path, text=False):
        assert Path(local_path).exists(), f"Path [{local_path}] does not exist"
        assert Path(s3_path), f"Invalid S3 Path [{s3_path}]"
        assert Path(
            local_path
        ).is_file(), f"Path [{local_path}] is not file. Only files are supported"
        s3_full_path = f"{s3_path}/{Path(local_path).name}"
        i = 0
        res = False
        cmd = f"aws s3 cp {local_path} s3://{s3_full_path}"
        if text:
            cmd += " --content-type text/plain"
        while not res and i < Settings.MAX_RETRIES_S3:
            i += 1
            res = Shell.check(
                cmd,
                verbose=True,
            )
        assert (
            res
        ), f"Failed to copy to s3 after Settings.MAX_RETRIES_S3 [{Settings.MAX_RETRIES_S3}] retries, file [{local_path}]"
        # TODO: add support for api gateway / cloudfront
        bucket = s3_path.split("/")[0]
        endpoint = Settings.S3_BUCKET_TO_HTTP_ENDPOINT[bucket]
        return f"https://{s3_full_path}".replace(bucket, endpoint)

    @classmethod
    def get_link(cls, s3_path, local_path):
        s3_full_path = f"{s3_path}/{Path(local_path).name}"
        bucket = s3_path.split("/")[0]
        endpoint = Settings.S3_BUCKET_TO_HTTP_ENDPOINT[bucket]
        return f"https://{s3_full_path}".replace(bucket, endpoint)

    @classmethod
    def copy_file_from_s3(cls, s3_path, local_path):
        assert Path(local_path), f"Path [{local_path}] does not exist"
        assert Path(s3_path), f"Invalid S3 Path [{s3_path}]"
        if Path(local_path).is_dir():
            local_path = Path(local_path) / Path(s3_path).name
        return Shell.check(
            f"aws s3 cp s3://{s3_path} {Path(local_path)}",
            verbose=True,
        )
