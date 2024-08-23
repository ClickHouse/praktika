import time

from praktika.settings import Settings, Environment
from praktika.utils import Shell


class GH:
    @classmethod
    def post_commit_status(cls, name, status, description, url):
        res = False
        retry_count = 0

        while retry_count < Settings.MAX_RETRIES_GH and not res:
            command = (
                f"gh api -X POST -H 'Accept: application/vnd.github.v3+json' "
                f"/repos/{Environment.REPOSITORY}/statuses/{Environment.EventInfo.REF_SHA} "
                f"-f state='{status}' -f target_url='{url}' "
                f"-f description='{description}' -f context='{name}'"
            )

            res = Shell.check(command, verbose=True)

            if not res:
                retry_count += 1
                time.sleep(5)

        if not res:
            print(
                f"ERROR: Failed to post commit status after [MAX_RETRIES_GH={Settings.MAX_RETRIES_GH}] attempts"
            )
            assert False
