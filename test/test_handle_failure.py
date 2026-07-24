"""
Unit tests for the sfn-io-helper failure handler.

handle_failure used to read sfn_state["Error"] directly. Fan-out state machines (for example
index-generation) catch with ResultPath "$.BatchJobError.<StateName>", so no top-level "Error"
key exists and the handler raised KeyError: 'Error' while trying to report the failure -- which
replaced the real error with an unrelated stack trace and made every fan-out failure look alike.
"""
import json
import os
import sys
import unittest

os.environ.setdefault("APP_NAME", "swipe-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "terraform", "modules", "sfn-io-helper-lambdas", "app",
    ),
)

import app  # noqa: E402


class TestFindFailure(unittest.TestCase):
    def test_top_level_error(self):
        # Linear pipelines catch to the top level.
        self.assertEqual(
            app.find_failure({"Error": "States.TaskFailed", "Cause": "boom"}),
            ("States.TaskFailed", "boom"),
        )

    def test_failure_result_path(self):
        # Catches wired to ResultPath "$.Failure".
        state = {"Failure": {"Error": "States.Timeout", "Cause": "slow"}}
        self.assertEqual(app.find_failure(state), ("States.Timeout", "slow"))

    def test_nested_batch_job_error_is_found(self):
        # Fan-out shape: ResultPath "$.BatchJobError.<StateName>", no top-level Error.
        state = {"BatchJobError": {"Phase2Lanes": {"Error": "UncaughtError", "Cause": "nope"}}}
        self.assertEqual(app.find_failure(state), ("UncaughtError", "nope"))

    def test_originating_task_preferred_over_parallel_wrapper(self):
        # The per-task entry is written before the Parallel state re-raises, so the task error
        # (the root cause) must win over the generic States.TaskFailed wrapper.
        state = {
            "BatchJobError": {
                "IndexTaxonomy": {"Error": "UncaughtError", "Cause": "real root cause"},
                "Phase2Lanes": {"Error": "States.TaskFailed", "Cause": "wrapper"},
            }
        }
        self.assertEqual(app.find_failure(state), ("UncaughtError", "real root cause"))

    def test_never_raises_when_no_error_anywhere(self):
        # Reporting a failure must not itself fail, even on an unexpected state shape.
        error, cause = app.find_failure({"Result": {}, "BatchJobError": {}})
        self.assertEqual(error, "UnknownError")
        self.assertIsInstance(cause, str)

    def test_tolerates_non_dict_members(self):
        state = {"Failure": "not-a-dict", "BatchJobError": {"A": None, "B": ["x"], "C": {"Error": "E"}}}
        self.assertEqual(app.find_failure(state), ("E", None))


class TestHandleFailure(unittest.TestCase):
    """handle_failure must raise a typed exception carrying the real error message."""

    def setUp(self):
        self._delete = app.stage_io.delete_restricted_intermediate_files
        app.stage_io.delete_restricted_intermediate_files = lambda *a, **kw: None

    def tearDown(self):
        app.stage_io.delete_restricted_intermediate_files = self._delete

    def test_fanout_failure_reports_root_cause_not_keyerror(self):
        # Regression: this is the real state shape from the index-generation run that failed in
        # GenerateIndexLineages. It used to raise KeyError: 'Error' from inside handle_failure.
        cause = json.dumps({"errorMessage": "KeyError: \"['superkingdom'] not in index\""})
        sfn_data = {
            "CurrentState": "HandleFailure",
            "Input": {"BatchJobError": {"Phase2Lanes": {"Error": "UncaughtError", "Cause": cause}}},
        }
        with self.assertRaises(Exception) as ctx:
            app.handle_failure(sfn_data, None)
        self.assertEqual(type(ctx.exception).__name__, "UncaughtError")
        self.assertIn("superkingdom", str(ctx.exception))

    def test_unknown_shape_still_raises(self):
        sfn_data = {"CurrentState": "HandleFailure", "Input": {"Result": {}}}
        with self.assertRaises(Exception) as ctx:
            app.handle_failure(sfn_data, None)
        self.assertEqual(type(ctx.exception).__name__, "UnknownError")


if __name__ == "__main__":
    unittest.main()
