import requests


class CheckRun:
    """Top-level workflow GitHub check run."""

    @staticmethod
    def _api(method, url, token, json_body=None):
        resp = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json_body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    @classmethod
    def start(cls, token, repo, head_sha, name):
        data = cls._api(
            "POST",
            f"https://api.github.com/repos/{repo}/check-runs",
            token,
            {
                "name": name,
                "head_sha": head_sha,
                "status": "in_progress",
                "actions": [
                    {
                        "label": "Cancel",
                        "description": "Cancel this CI run",
                        "identifier": "cancel",
                    }
                ],
            },
        )
        return cls(token, repo, data["id"], name)

    def __init__(self, token, repo, id, name):
        self.token = token
        self.repo = repo
        self.id = id
        self.name = name

    def complete(self, conclusion, output=None, details_url=None):
        body = {"status": "completed", "conclusion": conclusion}
        if output is not None:
            body["output"] = output
        if details_url is not None:
            body["details_url"] = details_url
        self._api(
            "PATCH",
            f"https://api.github.com/repos/{self.repo}/check-runs/{self.id}",
            self.token,
            body,
        )

    def update(self, output=None, details_url=None):
        body = {}
        if output is not None:
            body["output"] = output
        if details_url is not None:
            body["details_url"] = details_url
        if not body:
            return
        self._api(
            "PATCH",
            f"https://api.github.com/repos/{self.repo}/check-runs/{self.id}",
            self.token,
            body,
        )
