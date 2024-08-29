import dataclasses
import os

from praktika.utils import Shell


class Secret:

    class Type:
        AWS_SSM_VAR = "ssm"
        GH_SECRET = "gh secret"

    @dataclasses.dataclass
    class Config:
        name: str
        type: str
        encrypted: bool

    @classmethod
    def get_value(cls, config: "Secret.Config"):
        if config.type == Secret.Type.AWS_SSM_VAR:
            return cls.get_aws_ssm_var(config)
        elif config.type == Secret.Type.GH_SECRET:
            return cls.get_gh_secret(config)
        else:
            assert False, f"Not supported secret type, secret [{config}]"

    @classmethod
    def get_aws_ssm_var(cls, config):
        res = Shell.get_output_or_raise(
            f"aws ssm  get-parameter --name {config.name} --with-decryption --output text --query Parameter.Value",
        )
        return res

    @classmethod
    def get_gh_secret(cls, config):
        res = os.getenv(f"{config.name}")
        return res
