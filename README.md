# RecurCIPY

Pythonic CI on top of GitHub Actions

## How to begin:

```sh
# 1. install recurCIPY (TODO: python module is not yet there)
pip install recurcipy

# 2. checkout new branch
git checkout -b my_yaml_ci_written_in_python

# 3. create your workflow config in python or take any as an example from ./ci/config/*

# 4. generate pipeline files
python -m recurcipy --generate

# 5. Check pipeline files generated: ./.github/workflows/*.yaml

# 6. Commit and Push updates to remote:
git commit -m "Hello World"
git push --set-upstream origin my_yaml_ci_written_in_python

# 7. Create PR for the pushed branch

# 8. Enjoy Your Hello World CI
```

#### CI Platform features
|                         | GitHub | GitLab | BitBucket | comment                                   |
|-------------------------|--------|--------|-----------|-------------------------------------------|
| pull_request workflow   | Y      |        |           |                                           |
| push workflow           | Y      |        |           |                                           |
| merge_queue workflow    | N      |        |           |                                           |
| scheduled workflow      | N      |        |           |                                           |
| dispatch workflow       | N      |        |           |                                           |
| job artifacts           | Y      |        |           | Upload/download native platform artifacts |
| platform runners        | Y      |        |           | Free ubuntu-latest GH runner              |
| self-hosted runners     | Y      |        |           | Using your own CI runners (AWS EC2)       |

#### Cloud Compute features
|                         | AWS    | Azure | GCP   | comment                                        |
|-------------------------|--------|-------|-------|------------------------------------------------|
| EC2 as a CI runner      | Y      |       |       |                                                |
| ASG self-scale down     | Y      |       |       | Self scaling down upon job completion          |
| ASG self-scale up       | Y      |       |       | Requires 1+ EC2 instance in reserve            |
| S3 for artifacts        | N      |       |       |                                                |

#### Library features
|                         |     | comment                                                           |
|-------------------------|-----|-------------------------------------------------------------------|
| Pythonic CI pipelines   | Y   | 100% python interface for creating CI pipelines                   |
| Pre-requisites: python  | Y   | Install python dependencies as a pre-requisite job step           |
| Artifacts               | Y   | Upload/download artifacts in a pre/post job step (GH, S3)         |
| Reports                 | N   | Building HTML report for CI jobs                                  |
| ClickHouse CI DB        | N   | Export results to CI DB for analytics and observability           |
| CI Cache                | N   | Skip not-affected job/artifacts automatically                     |
| CI Customization        | N   | Support for manual CI customization within a CI run               |
| Docker as execution env | N   | Support running jobs in docker natively                           |
| Observability           | N   | Integration with observability platform, Grafana                  |
| Slack app               | N   | Slack app to subscribe to CI events, Alarms, etc                  |
| Automatic Backporting   | N   | Automatic PR backports to release ranches                         |