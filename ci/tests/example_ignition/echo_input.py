"""Mock native job for the GH_IGNITION dispatch demo.

Reads the workflow_dispatch input and fails if it did not propagate, proving the
path: GitHub UI input -> ignition job signs+POSTs -> lambda enqueues -> native
job reads it via Info.get_workflow_input_value.
"""
from praktika.info import Info

if __name__ == "__main__":
    paths = Info.get_workflow_input_value("paths")
    print(f"dispatch input 'paths' = {paths!r}")
    assert paths is not None, "expected 'paths' dispatch input in the native job"
    print(f"OK: dispatch input propagated; would run: ruff check {paths}")
