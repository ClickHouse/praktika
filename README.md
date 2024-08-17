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

#### CI Platform features:
|                       | GitHub | GitLab | comment                                   |
|-----------------------|--------|--------|-------------------------------------------|
| pull_request workflow | Y      | N      |                                           |
| push workflow         | Y      | N      |                                           |
| merge_queue workflow  | N, TBD | N      |                                           |
| scheduled workflow    | N, TBD | N      |                                           |
| job artifacts         | Y      | N      | upload/download native platform artifacts |
| platform runners      | Y      | N      | free ubuntu-latest GH runner              |
| self-hosted runners   | Y      | N      | using your own CI runners (AWS EC2)       |


#### Cloud Compute features
|                     | AWS    | Azure | comment                                         |
|---------------------|--------|-------|-------------------------------------------------|
| EC2 as a CI runner  | Y      | N     |                                                 |
| ASG auto scale down | Y      | N     | Self scaling down upon job completion           |
| ASG auto scale up   | Y      | N     | Requires 1+ EC2 instance in reserve permanently |
| Artifacts on s3     | N, TBD | N     |                                                 |

#### Library features
|                         |        | comment                                                                            |
|-------------------------|--------|------------------------------------------------------------------------------------|
| Pythonic CI pipelines   | Y      | 100% python interface to write pipelines for GH within supported platform features |
| CI cache                | N, TBD | Skip not-affected job/artifacts automatically                                      |
| CI customization        | N, TBD | Support for manual CI customization within a CI run                                |
| Docker as execution env | N, TBD | Support running jobs in docker natively                                            |
| Automatic job report    | N, TBD | building HTML report for CI jobs                                                   |
| ClickHouse CI DB        | N, TBD | results reporting to CI DB for analytics and observability                         |
| Observability           | N, TBD | Integration with observability platform, Grafana                                   |
| Slack app               | N, TBD | Slack app to subscribe to CI events, Posting alarms, notification                  |
| Automatic Backporting   | N, TBD | Automatic PR backports to release ranches                                          |