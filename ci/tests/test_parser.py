import unittest

from praktika.mangle import _get_workflows
from praktika.parser import WorkflowConfigParser
from praktika.settings import Settings
from praktika.version import current_praktika_version


class TestWorkflowConfigParser(unittest.TestCase):
    def test_parser(self):
        workflows = _get_workflows()
        for workflow in workflows:
            WorkflowConfigParser(workflow).parse()

    def test_default_local_workflow_can_be_selected_by_name(self):
        original = Settings.DEFAULT_LOCAL_TEST_WORKFLOW
        try:
            Settings.DEFAULT_LOCAL_TEST_WORKFLOW = "Praktika CI Advanced"
            workflows = _get_workflows(default=True, _for_validation_check=True)
        finally:
            Settings.DEFAULT_LOCAL_TEST_WORKFLOW = original

        self.assertEqual([workflow.name for workflow in workflows], ["Praktika CI Advanced"])

    def test_version_check_jobs_validate_expected_runtime_versions(self):
        workflows = {
            workflow.name: workflow
            for workflow in _get_workflows(_for_validation_check=True)
        }

        simple_version_check = workflows["Praktika CI"].get_job("Version Check")
        advanced_version_check = workflows["Praktika CI Advanced"].get_job(
            "Version Check"
        )

        self.assertIn("assert praktika == '0.1.4'", simple_version_check.command)
        self.assertEqual(simple_version_check.runs_on, ["arm-2xsmall-base"])
        self.assertIn(
            f"assert praktika == '{current_praktika_version()}'",
            advanced_version_check.command,
        )
