# Getting Started

This walks you from an empty repo to a working Praktika CI deployed on your own
AWS account, driven by a GitHub App. Commands run from your repo root.

Throughout, `<slug>` is your `PROJECT_SLUG` (set in `ci/settings/settings.py`,
default `praktika`); AWS resource names are prefixed with it.

## 1. Install

```bash
pip install 'praktika[infrastructure]'   # [infrastructure] pulls in the deploy deps
```

## 2. Set up AWS access

Create / get access to an AWS account and add a profile to `~/.aws/config` so
you can log in with:

```bash
aws sso login --profile <ProfileName>
```

Note `<ProfileName>` — you'll put it in the generated settings in step 4.

## 3. Scaffold the project

```bash
python3 -m praktika init
```

This generates a starter project you can run as-is or edit:

- `ci/workflows/` — your pipelines (jobs, triggers)
- `ci/infrastructure/projects.py` — AWS infra config (VPC, runner/orchestrator pools, proxies, …)
- `ci/settings/settings.py` — project-wide settings (slug, region, profile, sizing)

## 4. Review the config

Open `ci/settings/settings.py` and set `AWS_PROFILE` to the `<ProfileName>` from
step 2 and `AWS_REGION` to your region — Praktika uses them for every AWS call.
Then skim it and `ci/infrastructure/projects.py` and adjust naming and instance
sizing. The scaffold is deployable unchanged if you just want to see it work.

## 5. Deploy the infrastructure

```bash
python3 -m praktika infrastructure --deploy
```

**Expect to run this twice.** Runner/orchestrator pools (ASGs) launch from AMIs
that the deploy builds via **EC2 Image Builder**, which takes a while. The first
deploy kicks off the image pipelines and then can't finish the pools that depend
on them. Wait for the pipelines to go green (AWS console → **EC2 Image Builder →
Image pipelines**), then run `--deploy` again and let it complete.

The deploy also drops two files under `ci/infrastructure/` that you need next
(it prints their exact paths):

- `*_api_gateway.txt` — the webhook **Payload URL**
- `*.secret` — the auto-generated webhook **HMAC secret** (keep it safe)

…and creates an **empty** GitHub App secret `<slug>-gh-token` in AWS Secrets
Manager, with placeholder keys `app-id`, `app-key`, `app-installation-id` for you
to fill in at step 7.

## 6. Create and install the GitHub App

Create a GitHub App **in your org** (GitHub → org **Settings → Developer
settings → GitHub Apps → New**). Install it **only in that org**, granting access
to the target repo(s) — installing it anywhere else serves no purpose.

**Permissions** (minimum for CI):

| Permission | Access |
|---|---|
| Checks | Read & write |
| Contents | Read |
| Pull requests | Read |

Add more only for what your CI actually does — e.g. **Contents: write** (commits,
GitHub Pages), **Actions**, **Pages**.

**Webhook** — point it at the deploy output from step 5:

- **Payload URL**: contents of `*_api_gateway.txt`
- **Content type**: `application/json`
- **Secret**: contents of `*.secret`

Save, then confirm GitHub shows a successful delivery (green check in the App's
**Advanced** tab). Once the webhook verifies, subscribe to these **events**:
**Check run**, **Check suite**, **Pull request**, **Push**.

Finally, note the App's **App ID**, generate and download its **private key
(PEM)**, and note the **Installation ID** (in the install URL:
`…/settings/installations/<id>`).

## 7. Fill in the GitHub App secret

Put the three values from step 6 into the `<slug>-gh-token` secret created at
step 5 (AWS Secrets Manager), as JSON keys:

- `app-id` — the App ID **or** Client ID (GitHub accepts either as the JWT
  issuer; newer App settings show the **Client ID**, e.g. `Iv23li…`, and
  recommend it)
- `app-key` — the full PEM private key (including the header/footer lines)
- `app-installation-id` — the Installation ID

## 8. Verify

```bash
python3 -m praktika infrastructure --verify
```

Everything should report green. This mints a token through the App secret, probes
the deployed components, and reports the App installation's current API
rate-limit usage.

You're set — open a PR and Praktika CI runs against it.

---

### Tip: test safely without affecting other users' PRs

To try Praktika CI on your own PRs only, set a temporary allow-list on the
orchestrator pool in `ci/infrastructure/projects.py` and redeploy:

```python
OrchestratorPool(..., ext={"allowed_users": ["your-gh-handle"]})
```

With this set, `pull_request` events trigger CI only for the listed GitHub
logins. Remove it to open CI up to everyone.
