"""
SWIPE Step Function Helper Lambda

This is the source code for an AWS Lambda function that acts as part of a SWIPE AWS Step Functions state machine.

The helper Lambda performs the following functions:

- It prepares input for the WDL workflows by taking SFN input for each stage and saving it to S3 with common parameters.

- It loads AWS Batch job output into the step function state. The state machine dispatches Batch jobs to do the heavy
  lifting, but while Batch jobs can receive symbolic input via their command and environment variables, they cannot
  directly generate symbolic output. AWS Lambda can do that, so we have the Batch jobs upload their output as JSON
  to S3, and this function downloads and emits it as output. The state machine can then use this Lambda to load this
  data into its state.

- It acts as an I/O mapping adapter for legacy I/O names for different stages. The original workflows used implicit
  matching of filenames to map the outputs of one workflow to the inputs of the next. The WDL workflows require the
  mapping to be explicit, so we map the input and output names to resolve the value of the input to the next stage.

- It reacts to events emitted by the AWS Batch API whenever a new job enters RUNNABLE state. For all such events, it
  examines the state of the compute environment (CE) the job is being dispatched to, and adjusts the desiredVCPUs
  parameter for that CE to the number of vCPUs that it estimates is necessary. This is done to scale up the CE sooner
  than the Batch API otherwise would do so.

- It persists step function execution state to S3 to avoid losing this state after 90 days. To do this, it subscribes to
  events emitted by the AWS Step Functions API whenever a step function enters a RUNNING, SUCCEEDED, FAILED, TIMED_OUT,
  or ABORTED state. The state is saved to the OutputPrefix S3 directory under the `sfn-desc` and `sfn-hist` prefixes.

- It processes failures in the step function, forwarding error information and cleaning up any running Batch jobs.
"""
import os
import json
import logging

from sfn_io_helper import batch_events, reporting, stage_io

logging.getLogger().setLevel(logging.INFO)


def preprocess_input(sfn_data, _):
    assert sfn_data["CurrentState"] == "PreprocessInput"
    assert sfn_data["ExecutionId"].startswith("arn:aws:states:")
    assert len(sfn_data["ExecutionId"].split(":")) == 8
    _, _, _, aws_region, aws_account_id, _, state_machine_name, execution_name = sfn_data["ExecutionId"].split(":")
    return stage_io.preprocess_sfn_input(sfn_state=sfn_data["Input"],
                                         aws_region=aws_region,
                                         aws_account_id=aws_account_id,
                                         state_machine_name=state_machine_name)


def process_stage_output(sfn_data, _):
    assert sfn_data["CurrentState"].endswith("ReadOutput")
    stage_io.broadcast_stage_complete(
        sfn_data["ExecutionId"],
        sfn_data["CurrentState"][:-len("ReadOutput")],
    )
    sfn_state = stage_io.read_state_from_s3(sfn_state=sfn_data["Input"], current_state=sfn_data["CurrentState"])
    stage_io.link_outputs(sfn_state)
    sfn_state = stage_io.trim_batch_job_details(sfn_state=sfn_state)
    return sfn_state


def merge_parallel_outputs(sfn_data, _):
    # Additive helper for fan-out state machines that run stages inside an SFN `Parallel`
    # state. `Parallel` emits an array of per-branch states, each of which accumulated only
    # its own outputs into its Result. This unions those branch states back into one and then
    # runs the standard link_outputs, so downstream (post-join) stages resolve their inputs
    # from the whole run's Result exactly as in a linear pipeline. Linear state machines never
    # invoke this state, so their behaviour is unchanged.
    return stage_io.merge_parallel_outputs(sfn_state=sfn_data["Input"])


def handle_success(sfn_data, _):
    sfn_state = sfn_data["Input"]
    reporting.notify_success(sfn_state=sfn_state)
    stage_io.delete_restricted_intermediate_files(sfn_state)
    # stage_io.delete_sample_files(sfn_state)
    return sfn_state


def find_failure(sfn_state):
    """Locate the {"Error", "Cause"} pair describing why the execution failed.

    A Catch writes that pair to whatever its ResultPath points at, so where it lands depends on how
    the state machine is wired. Linear pipelines put it at the top level (or under "Failure"), but
    fan-out pipelines scope it per state, e.g. the index-generation machine catches with
    ResultPath "$.BatchJobError.<StateName>" for every task and Parallel state. In that shape there
    is no top-level "Error" at all, so reading it directly raised KeyError inside this handler and
    masked the real failure behind an unrelated stack trace. Prefer the most specific error we can
    find, and never raise while trying to report a failure.

    Returns a (error, cause) tuple; error is always a string, cause may be None.
    """
    for candidate in (sfn_state.get("Failure"), sfn_state):
        if isinstance(candidate, dict) and "Error" in candidate:
            return str(candidate["Error"]), candidate.get("Cause")
    # Fan-out shape: the per-state entries are written innermost-first, so the earliest entry
    # carrying an Error is the originating task rather than the Parallel state that re-raised it.
    batch_job_error = sfn_state.get("BatchJobError")
    if isinstance(batch_job_error, dict):
        for candidate in batch_job_error.values():
            if isinstance(candidate, dict) and "Error" in candidate:
                return str(candidate["Error"]), candidate.get("Cause")
    return "UnknownError", json.dumps(sfn_state, default=str)


def handle_failure(sfn_data, _):
    # This Lambda MUST raise an exception with the details of the error that caused the failure.
    sfn_state = sfn_data["Input"]
    assert sfn_data["CurrentState"] == "HandleFailure"
    reporting.notify_failure(sfn_state=sfn_state)
    # Clean up restricted intermediate files before propagating the failure,
    # so cleanup runs regardless of the terminal state of the execution.
    stage_io.delete_restricted_intermediate_files(sfn_state)
    # stage_io.delete_sample_files(sfn_state)
    error, cause = find_failure(sfn_state)
    failure_type = type(error, (Exception,), dict())
    try:
        cause = json.loads(cause)["errorMessage"]
    except Exception:
        pass
    raise failure_type(cause)


def process_batch_event(event, _):
    reporting.emit_batch_metric_values(event)


def process_sfn_event(event, _):
    execution_arn = event["detail"]["executionArn"]
    if os.environ["APP_NAME"] in execution_arn:
        batch_events.archive_sfn_history(execution_arn)

    reporting.emit_sfn_metric_values(event)


def report_metrics(_, __):
    reporting.emit_periodic_metrics()


def report_spot_interruption(event, _):
    reporting.emit_spot_interruption_metric(event)
