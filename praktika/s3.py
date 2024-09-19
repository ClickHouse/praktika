import dataclasses
import json
import time
from pathlib import Path
from typing import Dict

from praktika.utils import Shell
from praktika.settings import Settings


class S3:

    @dataclasses.dataclass
    class Object:
        AcceptRanges: str
        Expiration: str
        LastModified: str
        ContentLength: int
        ETag: str
        ContentType: str
        ServerSideEncryption: str
        Metadata: Dict

        def has_tags(self, tags):
            meta = self.Metadata
            for k, v in tags.items():
                if k not in meta or meta[k] != v:
                    print(f"tag [{k}={v}] does not match meta [{meta}]")
                    return False
            return True

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
        file_name = Path(local_path).name
        s3_full_path = s3_path
        if not s3_full_path.endswith(file_name):
            s3_full_path = f"{s3_path}/{Path(local_path).name}"
        cmd = f"aws s3 cp {local_path} s3://{s3_full_path}"
        if text:
            cmd += " --content-type text/plain"
        res = cls.run_command_with_retries(cmd)
        assert res
        bucket = s3_path.split("/")[0]
        endpoint = Settings.S3_BUCKET_TO_HTTP_ENDPOINT[bucket]
        assert endpoint
        return f"https://{s3_full_path}".replace(bucket, endpoint)

    @classmethod
    def put(cls, s3_path, local_path, text=False, metadata=None):
        assert Path(local_path).exists(), f"Path [{local_path}] does not exist"
        assert Path(s3_path), f"Invalid S3 Path [{s3_path}]"
        assert Path(
            local_path
        ).is_file(), f"Path [{local_path}] is not file. Only files are supported"
        file_name = Path(local_path).name
        s3_full_path = s3_path
        if not s3_full_path.endswith(file_name):
            s3_full_path = f"{s3_path}/{Path(local_path).name}"

        s3_full_path = str(s3_full_path).removeprefix("s3://")
        bucket, key = s3_full_path.split("/", maxsplit=1)

        command = (
            f"aws s3api put-object --bucket {bucket} --key {key} --body {local_path}"
        )
        if metadata:
            for k, v in metadata.items():
                command += f" --metadata {k}={v}"

        cmd = f"aws s3 cp {local_path} s3://{s3_full_path}"
        if text:
            cmd += " --content-type text/plain"
        res = cls.run_command_with_retries(command)
        assert res

    @classmethod
    def run_command_with_retries(cls, command):
        i = 0
        res = False
        while not res and i < Settings.MAX_RETRIES_S3:
            i += 1
            ret_code, stdout, stderr = Shell.get_res_stdout_stderr(
                command, verbose=True
            )
            if "aws sso login" in stderr:
                print("ERROR: aws login expired")
                break
            elif "does not exist" in stderr:
                print("ERROR: requested file does not exist")
                break
            if ret_code != 0:
                print(
                    f"ERROR: aws s3 cp failed, stdout/stderr err: [{stderr}], out [{stdout}]"
                )
            res = ret_code == 0
        return res

    @classmethod
    def get_link(cls, s3_path, local_path):
        s3_full_path = f"{s3_path}/{Path(local_path).name}"
        bucket = s3_path.split("/")[0]
        endpoint = Settings.S3_BUCKET_TO_HTTP_ENDPOINT[bucket]
        return f"https://{s3_full_path}".replace(bucket, endpoint)

    @classmethod
    def copy_file_from_s3(cls, s3_path, local_path):
        assert Path(s3_path), f"Invalid S3 Path [{s3_path}]"
        if Path(local_path).is_dir():
            local_path = Path(local_path) / Path(s3_path).name
        else:
            assert Path(
                local_path
            ).parent.is_dir(), f"Parent path for [{local_path}] does not exist"
        cmd = f"aws s3 cp s3://{s3_path}  {local_path}"
        res = cls.run_command_with_retries(cmd)
        return res

    @classmethod
    def head_object(cls, s3_path):
        s3_path = str(s3_path).removeprefix("s3://")
        bucket, key = s3_path.split("/", maxsplit=1)
        output = Shell.get_output(
            f"aws s3api head-object --bucket {bucket} --key {key}", verbose=True
        )
        if not output:
            return None
        else:
            return cls.Object(**json.loads(output))

    @classmethod
    def delete(cls, s3_path):
        assert Path(s3_path), f"Invalid S3 Path [{s3_path}]"
        return Shell.check(
            f"aws s3 rm s3://{s3_path}",
            verbose=True,
        )
