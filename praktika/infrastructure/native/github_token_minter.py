from dataclasses import dataclass, field
from typing import Dict, List

from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.lambda_function import Lambda


@dataclass
class GitHubTokenMinter:
    """Native component that deploys a Lambda which mints scoped GitHub App tokens.

    The lambda reads the GitHub App credentials from a single Secrets Manager
    secret and requests a fixed permission/repository scope configured via env.
    Callers only get the token scope this component is configured with.
    """

    permissions: Dict[str, str]
    repositories: List[str]
    secret_name: str
    region: str
    name: str = "praktika-gh-token"
    role_name: str = "praktika-gh-token-role"

    lambda_role: IAMRole.Config = field(init=False)
    lambda_config: Lambda.Config = field(init=False)

    def __post_init__(self):
        self.lambda_role = IAMRole.Config(
            name=self.role_name,
            trust_service="lambda.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ],
            inline_policies={
                "GitHubTokenMinterSecretsRead": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:DescribeSecret",
                                "secretsmanager:GetSecretValue",
                            ],
                            "Resource": f"arn:aws:secretsmanager:*:*:secret:{self.secret_name}*",
                        }
                    ],
                }
            },
        )
        self.lambda_config = Lambda.Config(
            name=self.name,
            path=__file__.replace("github_token_minter.py", "lambda_github_token.py"),
            handler="lambda_github_token.handler",
            region=self.region,
            role_name=self.role_name,
            environments={
                "GH_APP_SECRET_NAME": self.secret_name,
                "GH_TOKEN_PERMISSIONS_JSON": __import__("json").dumps(
                    self.permissions, sort_keys=True
                ),
                "GH_TOKEN_REPOSITORIES_JSON": __import__("json").dumps(
                    self.repositories
                ),
            },
            python_dependencies=[
                "PyJWT[crypto]>=2.10.0",
            ],
            timeout_ms=10 * 1000,
            memory_size_mb=128,
        )

    def grant_invoke(self, role: IAMRole.Config):
        policy = role.inline_policies.setdefault(
            "GitHubTokenMinterInvoke",
            {"Version": "2012-10-17", "Statement": []},
        )
        statement = {
            "Sid": f"Invoke{self.name.title().replace('-', '').replace('_', '')}",
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": f"arn:aws:lambda:*:*:function:{self.name}",
        }
        if statement not in policy["Statement"]:
            policy["Statement"].append(statement)
