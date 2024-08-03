# cipy

experimental max' repo

# To integrate recurcipy in your GitHub project

```sh
git checkout -b recurcipy

mkdir -p .github/workflows
touch github/workflows/pull_request.yml

cat>.github/workflows/pull_request.yml<<EOF
name: Pull Request

on:
  pull_request:
    branches:
      - main

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
    - name: Checkout code
      uses: actions/checkout@v3

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.x'

    - name: Install dependencies
      run: |
        python -m recurcipy
EOF

git add ./.github/workflows/pull_request.yml
```