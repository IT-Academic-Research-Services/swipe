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
from pathlib import Path
from unittest.mock import call, DEFAULT, patch

os.environ.setdefault("APP_NAME", "swipe-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
os.environ.setdefault("RESTRICTED_FILES", "[]")

# Add the parent directory to the path so the tests can import the app
APP_PATH = (Path(__file__).resolve().parents[1] / "terraform" / "modules" / "sfn-io-helper-lambdas" / "app")
sys.path.insert(0, str(APP_PATH))
import app
from sfn_io_helper import stage_io


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

    _key_error = KeyError("OutputPrefix")

    @patch.object(stage_io, "delete_restricted_intermediate_files")
    @patch.multiple('logging', exception=DEFAULT, info=DEFAULT, warning=DEFAULT)
    @patch.multiple("sentry_sdk", capture_exception=DEFAULT, capture_message=DEFAULT, isolation_scope=DEFAULT)
    def test_fanout_failure_reports_root_cause_not_keyerror(
            self, delete_restricted_intermediate_files, **multi_mocks
    ):
        # Regression: this is the real state shape from the index-generation run that failed in
        # GenerateIndexLineages. It used to raise KeyError: 'Error' from inside handle_failure.
        cause = json.dumps(
            {
                "errorMessage": "KeyError: \"['superkingdom'] not in index\"",
                "errorType": "KeyError",
                "stackTrace": ["  Trace Line 1", "  Trace Line 2"]
            }
        )
        sfn_data = {
            "CurrentState": "HandleFailure",
            "Input": {"BatchJobError": {"Phase2Lanes": {"Error": "UncaughtError", "Cause": cause}}},
        }
        with self.assertRaises(Exception) as ctx:
            app.handle_failure(sfn_data, None)
        self.assertEqual(type(ctx.exception).__name__, "UncaughtError")
        self.assertIn("superkingdom", str(ctx.exception))

        delete_restricted_intermediate_files.assert_called_once_with(sfn_data["Input"])
        multi_mocks['isolation_scope'].assert_called_once_with()
        multi_mocks['isolation_scope'].return_value.__enter__.return_value.set_context.assert_called_once_with(
            'details', {'cause': cause, 'sfn_data': sfn_data}
        )
        multi_mocks['capture_message'].assert_called_once_with('UncaughtError')
        multi_mocks['capture_exception'].assert_not_called()
        multi_mocks['exception'].assert_not_called()
        multi_mocks['info'].assert_not_called()
        multi_mocks['warning'].assert_not_called()

    @patch.object(stage_io, "delete_restricted_intermediate_files")
    @patch.multiple('logging', exception=DEFAULT, info=DEFAULT, warning=DEFAULT)
    @patch.multiple("sentry_sdk", capture_exception=DEFAULT, capture_message=DEFAULT, isolation_scope=DEFAULT)
    def test_unknown_shape_still_raises(
            self, delete_restricted_intermediate_files, **multi_mocks
    ):
        cause = {"Result": {}}
        sfn_data = {"CurrentState": "HandleFailure", "Input": cause}
        with self.assertRaises(Exception) as ctx:
            app.handle_failure(sfn_data, None)
        self.assertEqual(type(ctx.exception).__name__, "UnknownError")

        delete_restricted_intermediate_files.assert_called_once_with(sfn_data["Input"])
        multi_mocks['isolation_scope'].assert_called_once_with()
        multi_mocks['isolation_scope'].return_value.__enter__.return_value.set_context.assert_called_once_with(
            'details', {'cause': json.dumps(cause), 'sfn_data': sfn_data}
        )
        multi_mocks['capture_message'].assert_called_once_with('UnknownError')
        multi_mocks['capture_exception'].assert_not_called()
        multi_mocks['exception'].assert_not_called()
        multi_mocks['info'].assert_not_called()
        multi_mocks['warning'].assert_not_called()

    @patch.object(stage_io, "delete_restricted_intermediate_files", side_effect=_key_error)
    @patch.multiple('logging', exception=DEFAULT, info=DEFAULT, warning=DEFAULT)
    @patch.multiple("sentry_sdk", capture_exception=DEFAULT, capture_message=DEFAULT, isolation_scope=DEFAULT)
    def test_cleanup_failure_does_not_mask_real_error(
            self, delete_restricted_intermediate_files, **multi_mocks
    ):
        # SMP-1571 regression: cleanup raises KeyError('OutputPrefix');
        # the real cause is NonHostAlignment.
        os.environ["RESTRICTED_FILES"] = '["intermediate_.*"]'
        cause = json.dumps({"errorMessage": "chunk alignment failed"})
        sfn_data = {
            "CurrentState": "HandleFailure", "Input": {
                "Error": "NonHostAlignment", "Cause": cause,
            },
        }
        with self.assertRaises(Exception) as ctx:
            app.handle_failure(sfn_data, None)

        self.assertEqual(type(ctx.exception).__name__, "NonHostAlignment")
        self.assertEqual(str(ctx.exception), "chunk alignment failed")

        delete_restricted_intermediate_files.assert_called_once_with(sfn_data["Input"])
        multi_mocks['isolation_scope'].assert_has_calls(
            [
                call(),
                call().__enter__(),
                call().__enter__().set_context(
                    'details',
                    {'cause': '{"errorMessage": "chunk alignment failed"}', 'sfn_data': sfn_data}
                ),
                call().__exit__(None, None, None),
                call(),
                call().__enter__(),
                call().__enter__().set_context(
                    'details',
                    {'cause': 'delete_restricted_intermediate_files failed during handle_failure', 'sfn_data': sfn_data}
                ),
                call().__exit__(None, None, None),
            ],
            any_order=False
        )
        multi_mocks['capture_message'].assert_called_once_with('NonHostAlignment')
        multi_mocks['capture_exception'].assert_called_once_with(TestHandleFailure._key_error)
        multi_mocks['exception'].assert_has_calls(
            [
                call(
                    'delete_restricted_intermediate_files failed during handle_failure; continuing so the real stage error is propagated instead of being masked by the cleanup error.'
                ),
                call(
                    'send_exception_to_sentry called with kwargs %s',
                    '{"cause": "delete_restricted_intermediate_files failed during handle_failure", "sfn_data": {"CurrentState": "HandleFailure", "Input": {"Error": "NonHostAlignment", "Cause": "{\\"errorMessage\\": \\"chunk alignment failed\\"}"}}}',
                    exc_info=TestHandleFailure._key_error
                )
            ], any_order=False
        )
        multi_mocks['info'].assert_not_called()
        multi_mocks['warning'].assert_not_called()


if __name__ == "__main__":
    unittest.main()
