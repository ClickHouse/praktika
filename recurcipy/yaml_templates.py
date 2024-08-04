
class Templates:
    TEMPLATE_PULL_REQUEST_0 = """
name: {NAME}

on:
  {EVENT}:
    branches:
      - {BASE_BRANCH}

jobs:
{JOBS}
"""

    TEMPLATE_JOB = """
  {JOB_NAME}:
    needs: [{NEEDS}]
    uses: {AUX_WORKFLOW}
    with:
      job_name: {JOB_NAME}
{AUX_INPUT}
"""

    CALLABLE_JOB_TEMPLATE_0 = """
name: ReusableJob

'on':
  workflow_call:
    inputs:
      job_name:
        required: true
        type: string
{ADDONS_INPUTS}

jobs:
  Job:
    runs-on: ubuntu-latest
    name: ${{{{{{{{ inputs.job_name }}}}}}}}
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: DebugInfo
        run: |
          env | grep GITHUB

{ADDONS_STEPS}

      - name: Pre
        run: |
          python -m recurcipy.runner --pre-run --job-name "${{{{{{{{ inputs.job_name }}}}}}}}"

      - name: Run
        run: |
          python -m recurcipy.runner --run --job-name "${{{{{{{{ inputs.job_name }}}}}}}}"

      - name: Post
        run: |
          python -m recurcipy.runner --post-run --job-name "${{{{{{{{ inputs.job_name }}}}}}}}"
"""

    ADDON_PY_INPUT = """
      # add-on: python input
      requirements_txt:
        required: true
        type: string
"""

    ADDON_PY_STEPS = """
      # add-on: install python dependencies
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.9'
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r ${{ inputs.requirements_txt }}
          #pip install recurcipy
"""