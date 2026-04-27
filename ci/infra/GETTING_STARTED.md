# Getting Started

## 1. Initialize CI configuration

> TODO: describe `python3 -m praktika init` or equivalent scaffold command to generate `ci/` structure

## 2. Create GitHub App secrets in AWS Secrets Manager

The orchestrator and runners authenticate to GitHub via a GitHub App.

### 2a. Create a GitHub App

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Set permissions: `Actions: Read & Write`, `Contents: Read`, `Checks: Read & Write`, `Pull requests: Read`
3. Generate a **private key** (PEM file) and note the **App ID**
4. Install the app into your org/account and note the **Installation ID**
   (visible in the URL after installing: `github.com/organizations/<org>/settings/installations/<id>`)

### 2b. Store secrets in AWS Secrets Manager

The secret names below are **fixed** — the runner IAM policy grants access to
the `praktika-gh-app*` prefix, so the secrets must be named exactly as shown:

```bash
AWS_REGION=<your-region>

# Private key (paste PEM contents, including header/footer lines)
aws secretsmanager create-secret \
  --region "$AWS_REGION" \
  --name praktika-gh-app.app-key \
  --secret-string file://path/to/private-key.pem

# Installation ID (numeric string, e.g. "12345678")
aws secretsmanager create-secret \
  --region "$AWS_REGION" \
  --name praktika-gh-app.app-installation-id \
  --secret-string "<INSTALLATION_ID>"
```


## 3. Deploy infrastructure

### 3a. Publish the praktika package to S3

Orchestrators and runners install praktika from S3 at boot and before each run.
Build and upload the wheel whenever the package changes:

```bash
# Build
python3 -m pip install build --quiet
python3 -m build --wheel --outdir dist/

# Upload (the bucket and key are fixed — instances fetch from this exact URL)
aws s3 cp dist/praktika-0.1-py3-none-any.whl \
  s3://praktika-artifacts-eu-north-1/packages/praktika-0.1-py3-none-any.whl \
  --profile "$AWS_PROFILE"
```

### 3b. Deploy AWS infrastructure

```bash
python3 -m praktika infrastructure --deploy
```

This will:
- Create the SSM secret `praktika_gh_trigger_webhook_secret` (auto-generated, written to `ci/infra/praktika_gh_trigger_webhook_secret.secret`)
- Create the IAM role `praktika-gh-trigger-role` with `AWSLambdaBasicExecutionRole` and SQS send permissions
- Create the SQS queue `praktika-workflows` (workflow trigger inbox)
- Deploy the `praktika-gh-trigger` Lambda (GitHub webhook receiver)
- Create the API Gateway endpoint — URL is written to `ci/infra/praktika-gh-trigger_api_gateway.txt`
- Create the workflow orchestrator Launch Template and Auto Scaling Group
- Create runner pool Launch Template, Auto Scaling Group, and SQS queue

## 4. Configure GitHub Webhook

In your GitHub repository or organization go to **Settings → Webhooks → Add webhook** and set:

| Field | Value |
|---|---|
| Payload URL | URL from `ci/infra/praktika-gh-trigger_api_gateway.txt` |
| Content type | `application/json` |
| Secret | Value from `ci/infra/praktika_gh_trigger_webhook_secret.secret` |
| SSL verification | Enabled |

**Events to enable** (select individual events):
- Pull requests
- Pushes
- Check runs
- Check suites
- Merge groups *(skip if not used as a trigger)*

## 5. Next steps

> TODO: describe how to define workflows in `ci/workflows/`, add jobs, configure runner labels, etc.
