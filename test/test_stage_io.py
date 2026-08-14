import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_PATH = (
        Path(__file__).resolve().parents[1]
        / "terraform"
        / "modules"
        / "sfn-io-helper-lambdas"
        / "app"
)
sys.path.insert(0, str(APP_PATH))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from sfn_io_helper import stage_io  # noqa: E402


class DeleteRestrictedIntermediateFilesTest(unittest.TestCase):
    sfn_state = {
        "OutputPrefix": "s3://test-bucket/output",
        "RUN_WDL_URI": "s3://workflows/workflow-v1.2.3/run.wdl",
    }

    def test_rejects_non_string_regular_expression(self):
        with patch.dict(os.environ, {"RESTRICTED_FILES": json.dumps([1])}):
            with self.assertRaisesRegex(ValueError, "not a string"):
                stage_io.delete_restricted_intermediate_files(self.sfn_state)

    def test_batches_deletes_and_logs_response_errors(self):
        prefix = "output/workflow-1/"
        objects = [{"Key": f"{prefix}restricted-{i}.fastq"} for i in range(1001)]
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": objects}]
        bucket = MagicMock()
        bucket.delete_objects.side_effect = [
            {},
            {
                "Errors": [
                    {
                        "Key": objects[-1]["Key"],
                        "Code": "AccessDenied",
                        "Message": "denied",
                    }
                ]
            },
        ]
        mocked_s3 = MagicMock()
        mocked_s3.meta.client.get_paginator.return_value = paginator
        mocked_s3.Bucket.return_value = bucket

        restricted_files_environment = {
            "RESTRICTED_FILES": json.dumps([r".*\.fastq$"])
        }
        with patch.dict(os.environ, restricted_files_environment):
            with patch.object(stage_io, "s3", mocked_s3):
                with self.assertLogs(stage_io.logger, level="WARNING") as logs:
                    stage_io.delete_restricted_intermediate_files(self.sfn_state)

        self.assertEqual(2, bucket.delete_objects.call_count)
        first_batch = bucket.delete_objects.call_args_list[0].kwargs["Delete"]["Objects"]
        second_batch = bucket.delete_objects.call_args_list[1].kwargs["Delete"]["Objects"]
        self.assertEqual(1000, len(first_batch))
        self.assertEqual(1, len(second_batch))
        self.assertIn("AccessDenied", "\n".join(logs.output))
        self.assertIn(objects[-1]["Key"], "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
