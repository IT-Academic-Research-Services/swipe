"""
Unit tests for the sfn-io-helper success handler.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, patch

os.environ.setdefault("APP_NAME", "swipe-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
os.environ.setdefault("RESTRICTED_FILES", "[]")

# Add the parent directory to the path so the tests can import the app
APP_PATH = (Path(__file__).resolve().parents[1] / "terraform" / "modules" / "sfn-io-helper-lambdas" / "app")
sys.path.insert(0, str(APP_PATH))
import app
from sfn_io_helper import stage_io


class TestHandleSuccess(unittest.TestCase):
    """handle_success must not raise."""

    _key_error = KeyError("OutputPrefix")

    @patch.object(stage_io, "delete_restricted_intermediate_files")
    @patch.multiple('logging', exception=DEFAULT, info=DEFAULT, warning=DEFAULT)
    @patch.multiple("sentry_sdk", capture_exception=DEFAULT, capture_message=DEFAULT, isolation_scope=DEFAULT)
    def test_delete_intermediate_files_success(
            self, delete_restricted_intermediate_files, **multi_mocks
    ):
        # os.environ["RESTRICTED_FILES"] = '["intermediate_.*"]'
        sfn_data = {"CurrentState": "HandleFailure", "Input": {"Results": "Success"}}
        app.handle_success(sfn_data, None)

        delete_restricted_intermediate_files.assert_called_once_with(sfn_data["Input"])
        multi_mocks['isolation_scope'].assert_not_called()
        multi_mocks['capture_message'].assert_not_called()
        multi_mocks['capture_exception'].assert_not_called()
        multi_mocks['exception'].assert_not_called()
        multi_mocks['info'].assert_not_called()
        multi_mocks['warning'].assert_not_called()

    @patch.object(stage_io, "delete_restricted_intermediate_files", side_effect=_key_error)
    @patch.multiple('logging', exception=DEFAULT, info=DEFAULT, warning=DEFAULT)
    @patch.multiple("sentry_sdk", capture_exception=DEFAULT, capture_message=DEFAULT, isolation_scope=DEFAULT)
    def test_delete_intermediate_files_error(
            self, delete_restricted_intermediate_files, **multi_mocks
    ):
        # os.environ["RESTRICTED_FILES"] = '["intermediate_.*"]'
        sfn_data = {"CurrentState": "HandleFailure", "Input": {"Results": "Success", }}
        app.handle_success(sfn_data, None)

        delete_restricted_intermediate_files.assert_called_once_with(sfn_data["Input"])
        multi_mocks['isolation_scope'].assert_called_once_with()
        multi_mocks['isolation_scope'].return_value.__enter__.return_value.set_context.assert_called_once_with(
            'details',
            {'cause': 'delete_restricted_intermediate_files failed during handle_success', 'sfn_data': sfn_data}
        )
        multi_mocks['capture_exception'].assert_called_once_with(TestHandleSuccess._key_error)
        multi_mocks['capture_message'].assert_not_called()
        multi_mocks['exception'].assert_called_once_with(
            'delete_restricted_intermediate_files failed during handle_success; continuing as the job is not considered a failure simply because intermediate files were not deleted.'
        )
        multi_mocks['info'].assert_not_called()
        multi_mocks['warning'].assert_not_called()


if __name__ == "__main__":
    unittest.main()
