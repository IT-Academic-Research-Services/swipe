import os
import re
import json
import logging
from typing import List
from datetime import datetime
from uuid import uuid4

from botocore import xform_name
from botocore.exceptions import ClientError  # type: ignore

from . import s3, s3_object, sqs

logger = logging.getLogger()


def get_input_uri_key(stage):
    return f"{xform_name(stage).upper()}_INPUT_URI"


def get_output_uri_key(stage):
    return f"{xform_name(stage).upper()}_OUTPUT_URI"


def get_output_s3_uri(sfn_state):
    """
    Combines ``sfn_state['OutputPrefix']`` and ``workflow_name`` with the version transformed from ``<workflow>-vX.Y.Z`` suffix normalized to ``<workflow>-X``

    Example:
        ``OutputPrefix=s3://idseq-samples/samples/1/19/11``, ``workflow_name=short-read-mngs-v8.3.15`` -> ``s3://idseq-samples/samples/1/19/11/short-read-mngs-8``

    Returns:
        ``<OutputPrefix>/<transformed_workflow_name>`` with the suffix ``vX.Y.Z`` normalized to ``X``
    """
    output_s3_uri = sfn_state["OutputPrefix"]
    assert output_s3_uri.startswith("s3://")
    sub_path = re.sub(r"v(\d+)\..+", r"\1", get_workflow_name(sfn_state))
    return f"{output_s3_uri.rstrip('/')}/{sub_path}"


def get_stage_input(sfn_state, stage):
    input_uri = sfn_state[get_input_uri_key(stage)]
    return json.loads(s3_object(input_uri).get()["Body"].read().decode().strip() or '{}')


def put_stage_input(sfn_state, stage, stage_input):
    input_uri = sfn_state[get_input_uri_key(stage)]
    s3_object(input_uri).put(Body=json.dumps(stage_input).encode())


def get_stage_output(sfn_state, stage):
    output_uri = sfn_state[get_output_uri_key(stage)]
    try:
        return json.loads(s3_object(output_uri).get()["Body"].read().decode().strip() or '{}')
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return {}
        else:
            raise e


def read_state_from_s3(sfn_state, current_state):
    stage = current_state.replace("ReadOutput", "")
    sfn_state.setdefault("Result", {})
    stage_output = get_stage_output(sfn_state, stage)

    # Extract Batch job error, if any, and drop error metadata to avoid overrunning the Step Functions state size limit
    batch_job_error = sfn_state.pop("BatchJobError", {})
    # If the stage succeeded, don't throw an error
    if not sfn_state.get("BatchJobDetails", {}).get(stage):
        if batch_job_error and next(iter(batch_job_error)).startswith(stage):
            error_type = type(stage_output["error"], (Exception,), dict())
            raise error_type(stage_output.get("cause", stage_output.get("message")))

    # HACK: don't include list outputs due to the SFN state size limit
    sfn_state["Result"].update({k: v for k, v in stage_output.items() if not isinstance(v, list)})

    return sfn_state


def trim_batch_job_details(sfn_state):
    """
    Remove large redundant batch job description items from Step Function state to avoid overrunning the Step Functions
    state size limit.
    """
    sfn_state["BatchJobDetails"] = {k: {} for k in sfn_state["BatchJobDetails"]}
    return sfn_state


def segment_path(path: str) -> List[str]:
    _path = path
    segments: List[str] = []
    while _path:
        _path, segment = os.path.split(_path)
        segments.insert(0, segment)
    return segments


def get_workflow_name(sfn_state):
    """
    The workflow name is extracted from any ``*_WDL_URI`` entry in the SFN state.

    Example:
        ``HOST_FILTER_WDL_URI=s3://seqtoid-workflows-staging-030998640247/short-read-mngs-v8.3.15/host_filter.wdl`` -> ``short-read-mngs-v8.3.15``
    """
    for k, v in sfn_state.items():
        if k.endswith("_WDL_URI"):
            segments = [s for s in segment_path(v) if re.match(r".*-v(\d+)", s)]
            name = segments[0] if segments else os.path.basename(str(v))
            return os.path.splitext(name)[0]
    raise ValueError("Could not find workflow name")


def link_outputs(sfn_state):
    if len(list(sfn_state["Input"])) == 0:
        return

    stages_json_uri = sfn_state.get("STAGES_IO_MAP_JSON")
    stage_io_dict = {}
    if stages_json_uri:
        stage_io_dict = json.loads(s3_object(stages_json_uri).get()["Body"].read().decode().strip() or '{}')

    stripped_result = {k.split(".")[1]: v for k, v in sfn_state.get("Result", {}).items()}

    for stage in sfn_state["Input"].keys():
        stage_input = sfn_state["Input"][stage]
        for input_name, source in stage_io_dict.get(stage, {}).items():
            if isinstance(source, list):
                stage_input[input_name] = sfn_state["Input"].get(source[0], {}).get(source[1])
            elif source in stripped_result:
                stage_input[input_name] = stripped_result[source]
        put_stage_input(sfn_state=sfn_state, stage=stage, stage_input=stage_input)


def merge_parallel_outputs(sfn_state):
    """Union the branch states an SFN ``Parallel`` state emits, then link_outputs on the whole.

    A ``Parallel`` state runs each branch on an isolated copy of the state, so each branch's
    ``Result`` only holds the outputs of the stages IN that branch. When the branches join,
    SFN hands back an array of those per-branch states. This unions their ``Result`` (and
    ``BatchJobDetails``) into a single state -- restoring the whole run's accumulated ``Result``
    the way a linear pipeline would have it -- and then runs the ordinary ``link_outputs`` so
    the post-join stages resolve their inputs from every branch's outputs, cross-branch
    included. All the shared keys (``*_INPUT_URI``/``*_OUTPUT_URI``, ``STAGES_IO_MAP_JSON``,
    ``OutputPrefix``, memory) are identical across branches (set once by ``preprocess``), so the
    first branch is taken as the base.

    Expects ``sfn_state = {"Branches": [<branch state>, ...]}`` and returns the merged state.
    This is only reached by state machines that use a ``Parallel`` + merge state; linear ones
    never call it, so their behaviour is unchanged.
    """
    branches = sfn_state["Branches"]
    merged = dict(branches[0])
    merged_result = {}
    merged_batch_details = {}
    for branch in branches:
        merged_result.update(branch.get("Result", {}))
        merged_batch_details.update(branch.get("BatchJobDetails", {}))
    merged["Result"] = merged_result
    merged["BatchJobDetails"] = merged_batch_details
    link_outputs(merged)
    return merged


def preprocess_sfn_input(sfn_state, aws_region, aws_account_id, state_machine_name):
    # TODO: add input validation assertions here (use JSON schema?)
    output_path = get_output_s3_uri(sfn_state)

    for stage in sfn_state["Input"].keys():
        sfn_state[get_input_uri_key(stage)] = os.path.join(output_path, f"{xform_name(stage)}_input.json")
        sfn_state[get_output_uri_key(stage)] = os.path.join(output_path, f"{xform_name(stage)}_output.json")
        for compute_env in "SPOT", "EC2":
            memory_key = stage + compute_env + "Memory"
            memory_default_key = memory_key + "Default"
            if memory_default_key in os.environ:
                sfn_state.setdefault(memory_key, int(os.environ[memory_default_key]))
            vcpu_key = stage + compute_env + "Vcpu"
            vcpu_default_key = vcpu_key + "Default"
            if vcpu_default_key in os.environ:
                sfn_state.setdefault(vcpu_key, int(os.environ[vcpu_key + "Default"]))

    link_outputs(sfn_state)

    return sfn_state


def broadcast_stage_complete(execution_id: str, stage: str):
    if not os.environ.get("SQS_QUEUE_URLS"):
        return

    sqs_queue_urls = os.environ["SQS_QUEUE_URLS"].split(",")

    assert len(execution_id.split(":")) == 8
    _, _, _, aws_region, aws_account_id, _, state_machine_name, execution_name = execution_id.split(":")

    state_machine_arn = f"arn:aws:states:{aws_region}:{aws_account_id}:stateMachine:{state_machine_name}"

    body = json.dumps({
        "version": "0",
        "id": str(uuid4()),
        "detail-type": "Step Functions Execution Status Change",
        "source": "aws.states",
        "account": aws_account_id,
        "time": datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        "region": aws_region,
        "resources": [execution_id],
        "detail": {
            "executionArn": execution_id,
            "stateMachineArn": state_machine_arn,
            "name": execution_name,
            "status": "RUNNING",
            "lastCompletedStage": re.sub(r'(?<!^)(?=[A-Z])', '_', stage).lower(),
            # We don't set this because it isn't used yet and we don't have this
            #   field in the lambda, but it is part of the schema for these
            #   messages so we may need to add it.
            # "startDate": 1551225271984,
            "stopDate": None,
            "input": "{}",
            "inputDetails": {
                "included": None
            },
            "output": None,
            "outputDetails": None
        }
    })

    for squs_que_url in sqs_queue_urls:
        sqs.send_message(
            QueueUrl=squs_que_url,
            MessageBody=body,
        )


def delete_restricted_intermediate_files(sfn_state):
    """
    Delete all files listed in ``restricted_intermediate_files`` from the workflow's S3 output directory.

    Deletion errors are logged but never raised, so that a missing file does not stop this cleanup or impact the caller.
    """
    restricted_files_str = os.environ.get("RESTRICTED_FILES")
    if not restricted_files_str:
        raise ValueError("Could not load Environment Variable RESTRICTED_FILES")

    restricted_files_str = restricted_files_str.strip()
    if not restricted_files_str:
        raise ValueError("Environment Variable RESTRICTED_FILES is blank")

    try:
        restricted_files = json.loads(restricted_files_str)
    except ValueError as e:
        raise ValueError(f"Environment Variable RESTRICTED_FILES not valid JSON: [{restricted_files_str}] -> {e}")

    if not isinstance(restricted_files, list):
        raise ValueError(
            f"Environment Variable RESTRICTED_FILES not a list: "
            f"[{type(restricted_files).__name__}] {restricted_files}"
        )

    restricted_regexes = []
    for regex_str in restricted_files:
        if not isinstance(regex_str, str):
            raise ValueError(f"Restricted file Regular Expression is not a string: [{type(regex_str).__name__}] {regex_str}")
        try:
            regex = re.compile(regex_str)
        except re.error as e:
            raise ValueError(f"Restricted file Regular Expression is invalid: [{regex_str}] -> {e}")
        restricted_regexes.append(regex)

    s3_uri = get_output_s3_uri(sfn_state)
    bucket_name, prefix = s3_uri.split("/", 3)[2:]
    prefix = f"{prefix}/"

    logger.info(
        "Scanning for restricted intermediate files in %s/ (bucket=%s prefix=%s)",
        s3_uri,
        bucket_name,
        prefix
    )
    # We use the legacy paginator because it allows for retrieving only the files in a given directory, instead of recursing to all files
    paginator = s3.meta.client.get_paginator("list_objects_v2")
    objects_to_delete = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix, Delimiter="/"):
        if 'Contents' in page:
            for page_contents in page['Contents']:
                s3_key = page_contents['Key']
                if s3_key != prefix:
                    logger.debug("Trying to match S3 object s3://%s/%s", bucket_name, s3_key)
                    for restricted_regex in restricted_regexes:
                        if restricted_regex.fullmatch(s3_key):
                            objects_to_delete.append({"Key": s3_key})
                            break

    if objects_to_delete:
        logger.info("Deleting restricted intermediate files: %s", json.dumps(objects_to_delete))
        bucket = s3.Bucket(bucket_name)
        for batch_start in range(0, len(objects_to_delete), 1000):
            object_batch = objects_to_delete[batch_start:batch_start + 1000]
            try:
                response = bucket.delete_objects(Delete={"Objects": object_batch})
                for error in response.get("Errors", []):
                    logger.warning(
                        "Error deleting restricted intermediate file s3://%s/%s: [%s] %s",
                        bucket_name,
                        error.get("Key"),
                        error.get("Code"),
                        error.get("Message"),
                    )
            except Exception as e:
                logger.warning("Error deleting restricted intermediate files: %s", e)
    else:
        logger.info("No restricted intermediate files to delete")


# def delete_sample_files(sfn_state):
#     """
#     Delete all files listed in the workflow's S3 sample directory.
#
#     Deletion errors are logged but never raised so that a missing file does not impact the caller.
#     """
#
#     output_s3_uri = sfn_state["OutputPrefix"]
#     assert output_s3_uri.startswith("s3://")
#
#     # Remove the last part of the s3_uri and replace it with "fastqs/", including a terminating backslash
#     # IE: s3://idseq-samples/samples/1/19/11 -> bucket=idseq-samples prefix=samples/1/19/fastqs/
#     s3_uri_as_array = output_s3_uri.rstrip("/").split("/")[2:]
#     bucket_name = s3_uri_as_array[0]
#     prefix = "/".join([
#         *s3_uri_as_array[1:-1],
#         "fastqs",
#         ""
#     ])
#     s3_uri = f"s3://{bucket_name}/{prefix}"
#
#     logger.info("Deleting all files in %s (bucket=%s prefix=%s)", s3_uri, bucket_name, prefix)
#     try:
#         responses = s3.Bucket(bucket_name).objects.filter(Prefix=prefix).delete()
#         logger.info("Deleted sample files: %s", json.dumps(responses))
#     except Exception as e:
#         logger.warning("Unexpected error deleting sample files in %s -> %s", s3_uri, e)
