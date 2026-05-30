import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import boto3


JWT_TICK = 60
CACHE_TTL = 10 * 60
GITHUB_API_BASE = "https://api.github.com"
_CACHED = {"token": "", "expires_at": "", "permissions": {}, "fetched_at": 0.0}


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_env(name: str, expected_type):
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"Required Lambda environment variable {name} is empty")
    value = json.loads(raw)
    if not isinstance(value, expected_type):
        raise RuntimeError(
            f"Environment variable {name} must decode to {expected_type.__name__}"
        )
    return value


def _get_app_credentials():
    secret_name = os.environ.get("GH_APP_SECRET_NAME", "").strip()
    if not secret_name:
        raise RuntimeError("GH_APP_SECRET_NAME is not configured")
    client = boto3.session.Session().client(service_name="secretsmanager")
    secret = json.loads(
        client.get_secret_value(SecretId=secret_name)["SecretString"]
    )
    return (
        secret["app-key"],
        secret["app-id"],
        int(secret["app-installation-id"]),
    )


def _mint_jwt(private_key: str, app_id: str) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": int(time.time()) - JWT_TICK,
        "exp": int(time.time()) + (10 * JWT_TICK),
        "iss": str(app_id),
    }
    signing_input = (
        f"{_base64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_base64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
        fh.write(private_key)
        key_path = fh.name
    try:
        proc = subprocess.run(
            [
                "openssl",
                "dgst",
                "-binary",
                "-sha256",
                "-sign",
                key_path,
            ],
            input=signing_input.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"openssl signing failed: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        return f"{signing_input}.{_base64url(proc.stdout)}"
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass


def _mint_installation_token(jwt_token: str, installation_id: int):
    permissions = _json_env("GH_TOKEN_PERMISSIONS_JSON", dict)
    repositories = _json_env("GH_TOKEN_REPOSITORIES_JSON", list)
    body = {"permissions": permissions}
    if repositories:
        body["repositories"] = repositories
    request = urllib.request.Request(
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub access token request failed: HTTP {e.code}: {detail}"
        ) from e


def handler(event, context):
    _, _ = event, context
    if _CACHED["token"] and (time.time() - _CACHED["fetched_at"]) < CACHE_TTL:
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "token": _CACHED["token"],
                    "expires_at": _CACHED["expires_at"],
                    "permissions": _CACHED["permissions"],
                    "cached": True,
                }
            ),
        }

    private_key, app_id, installation_id = _get_app_credentials()
    jwt_token = _mint_jwt(private_key, app_id)
    token_info = _mint_installation_token(jwt_token, installation_id)

    _CACHED["token"] = token_info["token"]
    _CACHED["expires_at"] = token_info["expires_at"]
    _CACHED["permissions"] = token_info.get(
        "permissions", _json_env("GH_TOKEN_PERMISSIONS_JSON", dict)
    )
    _CACHED["fetched_at"] = time.time()

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "token": _CACHED["token"],
                "expires_at": _CACHED["expires_at"],
                "permissions": _CACHED["permissions"],
                "cached": False,
            }
        ),
    }
