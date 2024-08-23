# praktika

Resilient, feature-reach CI infrastructure on top of Git Management Platform (GH Action) and Cloud Provider (AWS WebServices).
It's easy with praktika.

### concepts
* 100% Tolerance to GitHub API Failures:
  * Make only the essential GitHub API calls, and limit them to the initial pipeline stage.
  * Ensure that all API calls are retryable in case of failure.
  * Provide GitHub data to CI jobs at runtime, eliminating the need for API calls during pipeline execution.
* Minimal Dependencies:
  * Prioritize using Python built-ins whenever possible.
  * Opt for Python standard libraries over external packages.
  * Only import non-standard modules if the user has enabled a feature that specifically requires them.
* Minimal Overhead:
  * Avoid unnecessary operations; only perform tasks explicitly requested by the user.
* Design Simplicity:
  * Favor a generic design, minimize custom handling.
  * Strive for a high "value per line of code" ratio, ensuring that each line of code provides maximum utility.

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

#### CI Platform features
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

#### Cloud Compute features
|                                     | AWS | Azure | GCP   | comment                               |
|-------------------------------------|-----|-------|-------|---------------------------------------|
| EC2 as a CI runner                  | Y   |       |       |                                       |
| ASG self-scale down                 | Y   |       |       | Self scaling down upon job completion |
| ASG self-scale up                   | Y   |       |       | Requires 1+ EC2 instance in reserve   |
| ASG zero-capacity-overhead scale up | N   |       |       | for instance: GH webhook + lambda     |
| ASG fixed size                      | Y   |       |       | no auto scaling                       |
| S3 for artifacts                    | Y   |       |       |                                       |
| CloudWatch runner logs              | N   |       |       |                                       |
| prebuild runner image (terraform)   | N   |       |       |                                       |

#### praktika features
|                         |               | comment                                                   |
|-------------------------|---------------|-----------------------------------------------------------|
| Pythonic CI pipelines   | Y             | 100% python interface for creating CI pipelines           |
| Pre-requisites: python  | Y             | Install python dependencies as a pre-requisite job step   |
| Artifacts               | Y             | Upload/download artifacts in a pre/post job step (GH, S3) |
| Reports                 | Y             | Building HTML report for CI jobs                          |
| ClickHouse CI DB        | N (High Prio) | Export results to CI DB for analytics and observability   |
| CI Cache                | Y             | Skip not-affected job, reuse artifacts                    |
| CI Customization        | N             | Support for manual CI customization within a CI run       |
| Docker as execution env | N             | Support running jobs in docker natively                   |
| Observability           | N             | Integration with observability platform, Grafana          |
| Slack app               | N             | Slack app to subscribe to CI events, Alarms, etc          |
| Automatic Backporting   | N             | Automatic PR backports to release ranches                 |
| Mergeable Check logic   | N (High Prio) | Automatic PR backports to release ranches                 |