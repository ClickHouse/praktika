import io
import json

from praktika.gh_auth import GHAuth
from praktika.settings import Settings


class _DummyLambdaClient:
    def __init__(self, payload):
        self._payload = payload
        self.invocations = []
        self.function_error = None

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        response = {"Payload": io.BytesIO(self._payload.encode("utf-8"))}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


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
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")

    token, expires_at = GHAuth.get_installation_token_with_expiry()

    assert token == "ghs_test_token"
    assert expires_at > 0
    assert client.invocations == [
        {
            "FunctionName": "gh-token",
            "InvocationType": "RequestResponse",
            "Payload": b"{}",
        }
    ]


def test_gh_auth_prefers_lambda_for_raw_token(monkeypatch):
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")
    monkeypatch.setattr(
        GHAuth,
        "_get_lambda_token_with_expiry",
        classmethod(
            lambda cls, required_permissions=None: ("ghs_lambda_token", 123.0)
        ),
    )

    assert GHAuth.get_installation_token() == "ghs_lambda_token"


def test_gh_auth_validates_required_lambda_permissions(monkeypatch):
    payload = json.dumps(
        {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "token": "ghs_test_token",
                    "expires_at": "2026-05-30T12:00:00Z",
                    "permissions": {"contents": "read"},
                }
            ),
        }
    )
    client = _DummyLambdaClient(payload)
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: client)
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")

    try:
        GHAuth.get_installation_token(
            required_permissions={"contents": "write"}
        )
        assert False, "expected permission failure"
    except RuntimeError as e:
        message = str(e)
        assert "contents=write (actual: read)" in message
        assert "ghs_test_token" not in message


def test_gh_auth_accepts_required_lambda_permissions(monkeypatch):
    payload = json.dumps(
        {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "token": "ghs_test_token",
                    "expires_at": "2026-05-30T12:00:00Z",
                    "permissions": {"contents": "write"},
                }
            ),
        }
    )
    client = _DummyLambdaClient(payload)
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: client)
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")

    assert (
        GHAuth.get_installation_token(
            required_permissions={"contents": "write"}
        )
        == "ghs_test_token"
    )


def test_gh_auth_redacts_lambda_error_payload(monkeypatch):
    client = _DummyLambdaClient('{"token":"should-not-leak"}')
    client.function_error = "Unhandled"
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: client)
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_NAME", "gh-token")
    monkeypatch.setattr(Settings, "GH_AUTH_LAMBDA_REGION", "eu-north-1")

    try:
        GHAuth.get_installation_token_with_expiry()
        assert False, "expected lambda auth failure"
    except RuntimeError as e:
        message = str(e)
        # The error surfaces whitelisted diagnostics (FunctionError, StatusCode,
        # errorType/errorMessage) via _describe_lambda_failure, but never the raw
        # lambda payload — so the token must not leak into the message.
        assert "FunctionError=Unhandled" in message
        assert "should-not-leak" not in message
