import os

import boto3
from botocore.config import Config

boto_config = Config(retries={'max_attempts': 10, 'mode': 'adaptive'})  # type: ignore

s3 = boto3.resource("s3", endpoint_url=os.getenv("AWS_ENDPOINT_URL"), config=boto_config)
batch = boto3.client("batch", endpoint_url=os.getenv("AWS_ENDPOINT_URL"), config=boto_config)
stepfunctions = boto3.client("stepfunctions", endpoint_url=os.getenv("AWS_ENDPOINT_URL"), config=boto_config)
cloudwatch = boto3.client("cloudwatch", endpoint_url=os.getenv("AWS_ENDPOINT_URL"), config=boto_config)
sqs = boto3.client("sqs", endpoint_url=os.getenv("AWS_ENDPOINT_URL"), config=boto_config)


def s3_object(uri):
    assert uri.startswith("s3://")
    bucket, key = uri.split("/", 3)[2:]
    return s3.Bucket(bucket).Object(key)


def paginate(boto3_paginator, *args, **kwargs):
    for page in boto3_paginator.paginate(*args, **kwargs):
        for result_key in boto3_paginator.result_keys:
            for value in page.get(result_key.parsed.get("value"), []):
                yield value
