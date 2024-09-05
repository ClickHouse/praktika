# praktika

Resilient, feature-reach CI infrastructure on top of Git Management Platform (GH Actions) and Public Cloud Provider (AWS WebServices).
It's easy with praktika.

### concepts:
* 100% Tolerance to GitHub API Failures:
  * Make only the essential GitHub API calls, and limit them to the initial pipeline stage.
  * Ensure that all API calls are retryable in case of failure.
  * Provide GitHub data to CI jobs at runtime, eliminating the need for API calls during pipeline execution.
* Early configuration fault detection:
  * Make pipeline configuration errors visible at pipeline generation step rather than runtime
* Minimal Dependencies:
  * Opt for Python standard libraries over external packages.
  * Only import non-standard modules if the user has enabled a feature that specifically requires them.
* Minimal Overhead:
  * Do not do more than explicitly requested by user.
* Design Simplicity:
  * Favor a generic design, minimize custom handling.

### dependencies:
* python
* non-standard python modules:
  * jwt module if GH App auth is needed. If HTML reporting and/or Mergeable check is enabled
* non-python dependencies:
  * aws cli. Not required for GH-only setup (without cloud provider)
  * gh cli. Not required for setup without HTML reporting and/or Mergeable check

## How to begin:

```sh
# 1. install recurCIPY (TODO: python module is not yet there)
pip install praktika

# 2. checkout new branch
git checkout -b my_praktika

# 3. create your workflow config in python or take any as an example from ./ci/config/*

# 4. generate pipeline files
python -m praktika --generate

# 5. Check pipeline files generated: ./.github/workflows/*.yaml

# 6. Commit and Push updates to remote:
git commit -m "Hello World"
git push --set-upstream origin my_praktika

# 7. Create PR for the pushed branch

# 8. Enjoy Your Hello World CI
```

#### praktika features
|                                       |   | comment                                                      |
|---------------------------------------|---|--------------------------------------------------------------|
| Pythonic CI pipelines                 | Y | 100% python interface for creating CI pipelines              |
| Artifacts                             | Y | Download/upload artifacts (GH, S3)                           |
| Reports                               | Y | HTML report for CI workflow/jobs/tests                       |
| CI Cache                              | Y | Skip not-affected job, reuse artifacts                       |
| Docker as execution env               | N | Support running jobs in docker natively                      |
| ClickHouse CI DB                      | Y | Export results to CI DB for analytics and observability      |
| Custom CI DB                          | N | Provide support for Bring Your Own CI DB                     |
| Mergeable Check logic                 | Y | Allow specific job(s) to fail without blocking merge         |
| CI Customization                      | N | Support for manual CI customization within a CI run          |
| Observability                         | N | Integration with observability platform, Grafana             |
| Collecting logs from runners          | N | system, logs, machine init logs, etc                         |
| Slack app                             | N | Slack app to subscribe to CI events, Alarms, etc             |
| Main CI Dashboard                     | N | Page comprising info about all running workflows/PRs/commits |
| Automatic Backporting                 | N | Automatic PR backports to release ranches                    |
| Pre-requisites: python                | Y | Install python dependencies as a pre-requisite job step      |
| Secret Management                     | Y | Fetch secrets from AWS SSM or GH secrets/variables           |
| Job Timeout config and handling       | N |                                                              |
| Matrix Jobs                           | N |                                                              |
| Collect execution logs on infra error | N | Collect runner, pre, post logs                               |

#### Supported GitHub features
|                       | GitHub | GitLab | BitBucket | comment                                   |
|-----------------------|--------|--------|-----------|-------------------------------------------|
| pull_request workflow | Y      |        |           |                                           |
| push workflow         | Y      |        |           |                                           |
| merge_queue workflow  | N      |        |           |                                           |
| scheduled workflow    | N      |        |           |                                           |
| dispatch workflow     | N      |        |           |                                           |
| Auth with App         | Y      |        |           |                                           |
| job artifacts         | Y      |        |           | Upload/download native platform artifacts |
| platform runners      | Y      |        |           | Free ubuntu-latest GH runner              |
| self-hosted runners   | Y      |        |           | Using your own CI runners (AWS EC2)       |
| secrets and variables | Y      |        |           | Using secrets in workflows                |

#### Cloud Compute features
|                                     | AWS | Azure | GCP   | comment                                                                    |
|-------------------------------------|-----|-------|-------|----------------------------------------------------------------------------|
| EC2 as a CI runner                  | Y   |       |       |                                                                            |
| ASG self-scale down                 | Y   |       |       | Self scaling down upon job completion                                      |
| ASG self-scale up                   | Y   |       |       | Requires 1+ EC2 instance in reserve                                        |
| ASG fixed size                      | Y   |       |       | no auto scaling                                                            |
| S3 for artifacts                    | Y   |       |       |                                                                            |
| ASG zero-capacity-overhead scale up | N   |       |       | for instance: GH webhook + lambda. (can be enabled, but no native support) |
| prebuild runner image (terraform)   | N   |       |       |                                                                            |
| SSM                                 | Y   |       |       | Using secrets from SSM in workflows                                        |
