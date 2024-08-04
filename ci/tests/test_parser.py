import unittest

from recurcipy.mangle import _get_workflows
from recurcipy.parser import WorkflowConfigParser


class TestWorkflowConfigParser(unittest.TestCase):

    def test_parser(self):
        workflows = _get_workflows()
        for workflow in workflows:
            WorkflowConfigParser(workflow).parse()
