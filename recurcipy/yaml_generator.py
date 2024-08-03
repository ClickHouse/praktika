from recurcipy.utils import Utils, Shell, cd

_TEMPLATE = """
name: {NAME}

on:
  {ON}:
    branches:
      - main

jobs:
  GenerateYaml:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          #pip install recurcipy

      - name: Generate yaml
        run: |
          python -m recurcipy
"""


class YamlGenerator:
    class WorkflowTrigers:
        PULL_REQUEST = "pull_request"

    def __init__(self):
        self.file_names = []
        pass

    @staticmethod
    def _get_file_name(workflow_name):
        return f"./.github/workflows/{Utils.normalize_string(workflow_name)}.yaml"

    def generate(self, name, on):
        file_name = self._get_file_name(name)
        with cd():
            with open(file_name, "w") as f:
                f.write(_TEMPLATE.format(NAME=name, ON=on))
        self.file_names.append(file_name)
        return file_name

    def push(self):
        assert self.file_names
        for file_name in self.file_names:
            Shell.check(f"git add {file_name}")
        Shell.check(f"git commit -m 'Generate: {self.file_names.join(' ')}'")
        Shell.check(f"git push")


if __name__ == '__main__':
    G = YamlGenerator()
    with cd():
        file_name = G.generate(name="Pull Request", on=YamlGenerator.WorkflowTrigers.PULL_REQUEST)
        Shell.check("git diff HEAD")

