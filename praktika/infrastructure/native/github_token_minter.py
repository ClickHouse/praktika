from dataclasses import dataclass, field
from typing import Any, Dict, List

from praktika.infrastructure._utils import aws_client
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.lambda_function import Lambda


# Keys the minting Lambda reads out of the Secrets Manager secret
# (see lambda_github_token.py::_get_app_credentials). deploy_secret()
# creates the secret with exactly these keys and empty string values so
# the secret exists for a human to fill in before the Lambda runs.
GITHUB_APP_SECRET_KEYS = ("app-id", "app-key", "app-installation-id")


DEFAULT_GITHUB_TOKEN_PERMISSIONS = {
    "checks": "write",
    "contents": "write",
    "issues": "write",
    "metadata": "read",
    "pages": "write",
    "pull_requests": "write",
    "statuses": "write",
}


@dataclass
class GitHubTokenMinter:
    """Native component that deploys a Lambda which mints scoped GitHub App tokens.

    The lambda reads the GitHub App credentials from a single Secrets Manager
    secret and requests a fixed permission/repository scope configured via env.
    Callers only get the token scope this component is configured with.
    """

    permissions: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_GITHUB_TOKEN_PERMISSIONS)
    )
    repositories: List[str] = field(default_factory=list)
    secret_name: str = "gh-app"
    region: str = ""
    name: str = "gh-token"
    role_name: str = "gh-token-role"
    ext: Dict[str, Any] = field(default_factory=dict)

    lambda_role: IAMRole.Config = field(init=False)
    lambda_config: Lambda.Config = field(init=False)

    def __post_init__(self):
        self._validate()
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
            # Keep one container warm and its in-container token cache fresh
            # (CACHE_TTL is 10 min in lambda_github_token.py). Callers invoke
            # this lambda synchronously on their own cold path, so a cold
            # container here (~4s to fetch the secret, mint the JWT and call
            # GitHub) lands directly on the caller's critical path. A periodic
            # ping keeps the token minting off that path (cache hit ~2ms).
            schedule_expression="rate(5 minutes)",
            timeout_ms=10 * 1000,
            memory_size_mb=128,
        )

    def _validate(self):
        if not self.permissions:
            raise ValueError("GitHubTokenMinter.permissions must not be empty")

    def deploy_secret(self):
        """Ensure the GitHub App secret exists in Secrets Manager.

        Idempotent and non-destructive: if the secret is absent it is created
        with the expected keys (GITHUB_APP_SECRET_KEYS) and empty string values
        so a human can fill in the real credentials. If it already exists it is
        left untouched, so manually-entered values and rotations are preserved.
        """
        import json

        sm = aws_client("secretsmanager", self.region, self.secret_name)
        try:
            sm.describe_secret(SecretId=self.secret_name)
            print(
                f"GitHub App secret '{self.secret_name}' already exists, skipping"
            )
            return self
        except sm.exceptions.ResourceNotFoundException:
            pass

        placeholder = {key: "" for key in GITHUB_APP_SECRET_KEYS}
        sm.create_secret(
            Name=self.secret_name,
            Description=(
                "GitHub App credentials for token minting "
                "(fill in app-id, app-key, app-installation-id)"
            ),
            SecretString=json.dumps(placeholder),
        )
        print(
            f"Created empty GitHub App secret '{self.secret_name}' with keys "
            f"{', '.join(GITHUB_APP_SECRET_KEYS)} — set their values in "
            f"AWS Secrets Manager before the minter Lambda runs"
        )
        return self

    def apply_defaults(self, default_repository: str = ""):
        if not self.repositories and default_repository:
            self.repositories = [default_repository]
            self.lambda_config.environments["GH_TOKEN_REPOSITORIES_JSON"] = __import__(
                "json"
            ).dumps(self.repositories)
        if not self.repositories:
            raise ValueError(
                "GitHubTokenMinter.repositories must be set, or CloudInfrastructure.Config.name "
                "must provide the default repository scope"
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
