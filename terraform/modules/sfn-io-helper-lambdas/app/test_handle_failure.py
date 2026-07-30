"""Standalone unit test for SMP-1571: the HandleFailure cleanup must not mask the real stage error.
Run: python terraform/modules/sfn-io-helper-lambdas/app/test_handle_failure.py
"""
import sys, os, types, json
from unittest import mock

# stub boto3 so app / stage_io / reporting import without AWS
b = types.ModuleType("boto3"); b.client = lambda *a, **k: None; b.resource = lambda *a, **k: None
sys.modules["boto3"] = b
# reporting.py binds these at import time (module-level default args); set before importing app.
os.environ.setdefault("APP_NAME", "test-app")
os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sfn_io_helper import stage_io  # noqa: E402
import app  # noqa: E402

# ---------------------------------------------------------------------------
# 1) get_output_s3_uri's missing-OutputPrefix path no longer raises out of cleanup.
#    On the failure path the state does not carry OutputPrefix; cleanup must skip, not KeyError.
os.environ["RESTRICTED_FILES"] = '["intermediate_.*"]'
# Must return None (skip) and NOT raise, even though OutputPrefix is absent.
result = stage_io.delete_restricted_intermediate_files({"SomethingElse": 1})
assert result is None, f"expected cleanup to skip and return None, got {result!r}"
print("OK: delete_restricted_intermediate_files skips gracefully when OutputPrefix is missing")

# ---------------------------------------------------------------------------
# 2) handle_failure propagates the REAL stage error even when cleanup blows up.
#    Simulate the exact SMP-1571 shape: cleanup raises KeyError('OutputPrefix'); the real cause is
#    a NonHostAlignment 'chunk alignment failed'. handle_failure must raise the REAL error, not the
#    KeyError.
sfn_data = {
    "CurrentState": "HandleFailure",
    "Input": {
        "Error": "NonHostAlignment",
        "Cause": json.dumps({"errorMessage": "chunk alignment failed"}),
    },
}
with mock.patch.object(app.reporting, "notify_failure", return_value=None), \
     mock.patch.object(app.stage_io, "delete_restricted_intermediate_files",
                       side_effect=KeyError("OutputPrefix")):
    raised = None
    try:
        app.handle_failure(sfn_data, None)
    except BaseException as e:  # noqa: BLE001 - we assert on the type/message below
        raised = e

assert raised is not None, "handle_failure must raise"
assert not isinstance(raised, KeyError), (
    f"handle_failure masked the real error with the cleanup KeyError: {raised!r}"
)
assert type(raised).__name__ == "NonHostAlignment", (
    f"expected the real error type 'NonHostAlignment', got {type(raised).__name__}"
)
assert str(raised) == "chunk alignment failed", (
    f"expected the real cause message, got {str(raised)!r}"
)
print("OK: handle_failure propagates the real stage error even when cleanup raises")

print("\nALL PASSED")
