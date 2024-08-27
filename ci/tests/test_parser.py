import unittest

from praktika.mangle import _get_workflows
from praktika.parser import WorkflowConfigParser


class TestWorkflowConfigParser(unittest.TestCase):
    def test_parser(self):
        workflows = _get_workflows()
        for workflow in workflows:
            WorkflowConfigParser(workflow).parse()
