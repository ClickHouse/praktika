import unittest

from praktika.mangle import _get_workflows
from praktika.parser import WorkflowConfigParser
from praktika.settings import Settings


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
