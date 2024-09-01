import time
from pathlib import Path

import requests
from jwt import jwk_from_pem, JWT

from praktika.environment import Environment
from praktika.utils import Shell


class Auth:
    @staticmethod
    def _generate_jwt(client_id, pem):
        if Path(pem).exists():
            with open(pem, "rb") as pem_file:
                pem = pem_file.read()
        else:
            pem = str.encode(pem)
        signing_key = jwk_from_pem(pem)
        payload = {
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "iss": client_id,
        }
        # Create JWT
        jwt_instance = JWT()
        encoded_jwt = jwt_instance.encode(payload, signing_key, alg="RS256")
        return encoded_jwt

    @staticmethod
    def _get_installation_id(jwt_token):
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = requests.get(
            "https://api.github.com/app/installations", headers=headers, timeout=10
        )
        response.raise_for_status()
        installations = response.json()
        assert installations, "No installations found for the GitHub App"
        return installations[0]["id"]

    @staticmethod
    def _get_access_token(jwt_token, installation_id):
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        url = (
            f"https://api.github.com/app/installations/{installation_id}/access_tokens"
        )
        response = requests.post(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()["token"]

    @classmethod
    def auth(cls) -> None:
        pem = Environment.get().SECRET_APP_PEM_KEY
        assert pem
        # Generate JWT
        jwt_token = cls._generate_jwt(Environment.get().SECRET_APP_ID, pem)
        # Get Installation ID
        installation_id = cls._get_installation_id(jwt_token)
        # Get Installation Access Token
        access_token = cls._get_access_token(jwt_token, installation_id)
        Shell.check(f"echo {access_token} | gh auth login --with-token", strict=True)


if __name__ == "__main__":
    Auth.auth()
