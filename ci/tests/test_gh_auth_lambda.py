import io
import json

from praktika.gh_auth import GHAuth
from praktika.settings import Settings


class _DummyLambdaClient:
    def __init__(self, payload):
        self._payload = payload
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"Payload": io.BytesIO(self._payload.encode("utf-8"))}


def test_gh_auth_uses_lambda_response(monkeypatch):
    payload = json.dumps(
        {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "token": "ghs_test_token",
                    "expires_at": "2026-05-30T12:00:00Z",
                    "permissions": {"checks": "write"},
                }
            ),
        }
    )
    client = _DummyLambdaClient(payload)
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: client)
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "praktika-gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")

    token, expires_at = GHAuth.get_installation_token_with_expiry()

    assert token == "ghs_test_token"
    assert expires_at > 0
    assert client.invocations == [
        {
            "FunctionName": "praktika-gh-token",
            "InvocationType": "RequestResponse",
            "Payload": b"{}",
        }
    ]


def test_gh_auth_prefers_lambda_for_raw_token(monkeypatch):
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "praktika-gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")
    monkeypatch.setattr(
        GHAuth,
        "_get_lambda_token_with_expiry",
        classmethod(lambda cls: ("ghs_lambda_token", 123.0)),
    )

    assert GHAuth.get_installation_token() == "ghs_lambda_token"
