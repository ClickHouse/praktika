
class Templates:
    TEMPLATE_PULL_REQUEST_0 = """\
name: {NAME}

on:
  {EVENT}:
    branches:
      - {BASE_BRANCH}

jobs:
{JOBS}\
"""

    TEMPLATE_JOB_0 = """
  {JOB_NAME_NORMALIZED}:
    runs-on: ubuntu-latest
    needs: [{NEEDS}]
    name: {JOB_NAME}
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up env
        run: |
{SETUP_ENVS}
          env | grep GITHUB
{JOB_ADDONS}{DOWNLOADS_GITHUB}
      - name: Pre
        run: |
          python -m recurcipy.runner --pre-run --job-name "{JOB_NAME}" --workflow-name "{WORKFLOW_NAME}"

      - name: Run
        run: |
          python -m recurcipy.runner --run --job-name "{JOB_NAME}" --workflow-name "{WORKFLOW_NAME}"

      - name: Post
        run: |
          python -m recurcipy.runner --post-run --job-name "{JOB_NAME}" --workflow-name "{WORKFLOW_NAME}"
{UPLOADS_GITHUB}\
"""

    TEMPLATE_SETUP_ENV = """\
          rm -rf {INPUT_DIR} {OUTPUT_DIR} {TEMP_DIR}
          mkdir -p {TEMP_DIR} {INPUT_DIR} {OUTPUT_DIR}
          echo "TEMP_DIR=$(readlink -f {TEMP_DIR})" >> "$GITHUB_ENV"
          echo "INPUT_DIR=$(readlink -f {INPUT_DIR})" >> "$GITHUB_ENV"
          echo "OUTPUT_DIR=$(readlink -f {OUTPUT_DIR})" >> "$GITHUB_ENV"\
"""

    TEMPLATE_PY_ADDONS = """
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r {REQUIREMENT_PATH}
          #pip install recurcipy
"""

    TEMPLATE_GH_UPLOAD = """
      - name: Upload artifact {NAME}
        uses: actions/upload-artifact@v3
        with:
          name: {NAME}
          path: {PATH}
"""

    TEMPLATE_GH_DOWNLOAD = """
      - name: Download artifact {NAME}
        uses: actions/download-artifact@v3
        with:
          name: {NAME}
          path: {PATH}
"""
#
#     TEMPLATE_JOB_NESTED = """\
#   {JOB_NAME}:
#     needs: [{NEEDS}]
#     uses: {AUX_WORKFLOW}
#     with:
#       job_name: {JOB_NAME}
# {AUX_INPUT}\
# """
#
#     CALLABLE_JOB_TEMPLATE_0 = """\
# name: ReusableJob
#
# 'on':
#   workflow_call:
#     inputs:
#       job_name:
#         required: true
#         type: string
# {ADDONS_INPUTS}
#
# jobs:
#   Job:
#     runs-on: ubuntu-latest
#     name: ${{{{{{{{ inputs.job_name }}}}}}}}
#     steps:
#       - name: Checkout code
#         uses: actions/checkout@v4
#
#       - name: DebugInfo
#         run: |
#           env | grep GITHUB
#
# {ADDONS_STEPS}
#
#       - name: Pre
#         run: |
#           python -m recurcipy.runner --pre-run --job-name "${{{{{{{{ inputs.job_name }}}}}}}}"
#
#       - name: Run
#         run: |
#           python -m recurcipy.runner --run --job-name "${{{{{{{{ inputs.job_name }}}}}}}}"
#
#       - name: Post
#         run: |
#           python -m recurcipy.runner --post-run --job-name "${{{{{{{{ inputs.job_name }}}}}}}}"\
# """
#
#     ADDON_PY_INPUT = """
#       # add-on: python input
#       requirements_txt:
#         required: true
#         type:
# """

