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
        encrypted: bool = False

        def is_gh(self):
            return self.type == Secret.Type.GH_SECRET

        def get_value(self):
            if self.type == Secret.Type.AWS_SSM_VAR:
                return self.get_aws_ssm_var()
            elif self.type == Secret.Type.GH_SECRET:
                return self.get_gh_secret()
            else:
                assert False, f"Not supported secret type, secret [{self}]"

        def get_aws_ssm_var(self):
            res = Shell.get_output_or_raise(
                f"aws ssm  get-parameter --name {self.name} --with-decryption --output text --query Parameter.Value",
            )
            return res

        def get_gh_secret(self):
            res = os.getenv(f"{self.name}")
            return res

        def __repr__(self):
            return self.name
