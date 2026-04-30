"""Job script run by ci/workflows/_dummy_test_workflow.py from test_runner.py."""
from praktika.result import Result

if __name__ == "__main__":
    Result.get().set_success().set_info("dummy job ok")
