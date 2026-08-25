#!/bin/bash
set -a
if [ -f /etc/environment ]; then source /etc/environment; fi
if [ -f /etc/default/locale ]; then source /etc/default/locale; else export LC_ALL=C.UTF-8 LANG=C.UTF-8; fi
set +a

if [ -n "${AWS_ENDPOINT_URL-}" ]; then
  export aws="aws --endpoint-url ${AWS_ENDPOINT_URL}"
  export S3PARCP_S3_URL="${AWS_ENDPOINT_URL}"
else
  export aws="aws"
fi

# Marker file written whenever we detect that this chunk's host is being reclaimed (spot
# instance-action seen on IMDS, or a SIGTERM delivered to this entrypoint). handle_error consults
# it to re-surface an otherwise-swallowed spot interruption as a retryable failure. See the long
# comment in handle_error for why this is necessary.
SPOT_INTERRUPTION_MARKER=/tmp/swipe_spot_interruption

# If the ECS/Batch agent (or a spot reclamation) sends SIGTERM to this entrypoint, record it so the
# EXIT trap can classify the resulting miniwdl failure as a retryable host reclamation rather than a
# genuine tool error. bash defers this trap until the foreground `miniwdl run` returns, which is
# exactly when we want to inspect the marker.
handle_sigterm() {
  echo WARNING: entrypoint received SIGTERM, treating as spot/host reclamation >> /dev/stderr
  touch "$SPOT_INTERRUPTION_MARKER"
}
trap handle_sigterm SIGTERM

check_for_termination() {
  count=0
  while true; do
    if TOKEN=`curl -m 10 -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600"` && curl -H "X-aws-ec2-metadata-token: $TOKEN" -sf http://169.254.169.254/latest/meta-data/spot/instance-action; then
      echo WARNING: THIS SPOT INSTANCE HAS BEEN SCHEDULED FOR TERMINATION >> /dev/stderr
      # Record the reclamation so handle_error can re-surface miniwdl's swallowed SIGTERM as a
      # retryable failure even though miniwdl exits with a clean, non-retryable-looking code.
      touch "$SPOT_INTERRUPTION_MARKER"
    fi
    # Print an update every 5 mins
    if [ $((count++ % 60)) -eq 0 ]; then
      echo $(date --iso-8601=seconds) termination check
    fi
    sleep 10
  done
}

put_metric() {
    $aws cloudwatch put-metric-data --metric-name $1 --namespace $APP_NAME --unit Percent --value $2 --dimensions SFNCurrentState=$SFN_CURRENT_STATE
}

put_metrics() {
  while true; do
    put_metric ScratchSpaceInUse $(df --output=pcent $MINIWDL_DIR | tail -n 1 | cut -f 1 -d %)
    put_metric CPULoad $(cat /proc/loadavg | cut -f 1 -d ' ' | cut -f 2 -d .)
    put_metric MemoryInUse $(python3 -c 'import psutil; m=psutil.virtual_memory(); print(100*(1-m.available/m.total))')
    sleep 60
  done
}

check_for_termination &
put_metrics &

mkdir -p $MINIWDL_DIR/download_cache; touch $MINIWDL_DIR/download_cache/_miniwdl_flock

clean_wd() {
  (shopt -s nullglob;
  for wf_log in $MINIWDL_DIR/20??????_??????_*/workflow.log; do
    flock -n $wf_log rm -rf $(dirname $wf_log) || true;
  done;
  flock -x $MINIWDL_DIR/download_cache/_miniwdl_flock clean_download_cache.sh $MINIWDL_DIR/download_cache $DOWNLOAD_CACHE_MAX_GB)
}
clean_wd
df -h / $MINIWDL_DIR
export MINIWDL__S3_PROGRESSIVE_UPLOAD__URI_PREFIX=$(dirname "$WDL_OUTPUT_URI")

if [ -f /etc/profile ]; then source /etc/profile; fi
miniwdl --version
# Env vars that need to be forwarded to miniwdl's tasks in AWS Batch.
BATCH_SWIPE_ENVVARS="AWS_DEFAULT_REGION AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_ENDPOINT_URL S3PARCP_S3_URL"
# set $WDL_PASSTHRU_ENVVARS to a list of space-separated env var names
# to pass the values of those vars to miniwdl's task containers.
PASSTHRU_VARS=( $BATCH_SWIPE_ENVVARS $WDL_PASSTHRU_ENVVARS )
PASSTHRU_ARGS=${PASSTHRU_VARS[@]/#/--env }

set -euo pipefail
export CURRENT_STATE=$(echo "$SFN_CURRENT_STATE" | sed -e s/SPOT// -e s/EC2//)

$aws s3 cp "$WDL_WORKFLOW_URI" .
$aws s3 cp "$WDL_INPUT_URI" wdl_input.json

handle_error() {
  # Add enhanced logging for our most common termination types
  EXIT_CODE=$?

  # Spot-reclaim re-surfacing (root cause of the "alignment chunk fails, passes on manual re-run"
  # class of sample failures):
  #
  # When AWS reclaims a spot host, the ECS/Batch agent SIGTERMs the containers on it. miniwdl traps
  # that signal, aborts its running docker task (logs `error: "Interrupted"`, docker task exit
  # code = -1) and exits CLEANLY with code 2. To AWS Batch that is indistinguishable from a genuine
  # tool failure -- statusReason "Essential container in task exited", container exitCode 2. Because
  # it is not the string "Host EC2 (instance i-*) terminated." and not exit 143, NONE of the
  # host-reclamation retry rules fire: not the job-def retryStrategy (Host EC2*), not the SFN
  # RunDetectError choice, and not the fan-out coordinator's _classify_terminal_failure (which
  # retries None/143/host-terminated but treats a bare exit 2 as a real, non-retryable error). The
  # chunk dies after one attempt and the whole sample fails, even though a plain re-run succeeds.
  #
  # Fix: if our detector tripped (IMDS spot instance-action seen, or we caught SIGTERM directly) and
  # miniwdl exited with one of the CLEAN-looking codes it produces when it swallows the SIGTERM
  # (1 or 2), re-surface the failure as 143 (SIGTERM) so every one of those retry rules classifies it
  # as a retryable host reclamation. We deliberately do NOT touch the memory-kill signals (137 SIGKILL
  # / 139 segfault / 134 abort) or an already-143 exit: those already carry correct, retryable
  # meanings downstream (137/139/134 -> escalate memory, 143 -> retry on-demand), so leaving them
  # alone avoids mislabeling a genuine OOM as a spot reclamation. Genuine tool errors -- where no
  # interruption was detected -- keep their original exit code and stay non-retryable, preserving
  # normal failure semantics.
  if [[ -f "$SPOT_INTERRUPTION_MARKER" && ( $EXIT_CODE == 1 || $EXIT_CODE == 2 ) ]]; then
    echo "ERROR: chunk interrupted by spot/host reclamation (original exit $EXIT_CODE); re-surfacing as SIGTERM/143 so retry rules fire" >> /dev/stderr
    EXIT_CODE=143
  fi

  if [[ $EXIT_CODE == 137 ]]; then
    echo ERROR: container terminated with SIGKILL, this is most likely because memory usage was above container limits >> /dev/stderr
    exit $EXIT_CODE
  fi

  if [[ $EXIT_CODE == 143 ]]; then
    echo ERROR: container terminated with SIGTERM, this is most likely because of a spot instance termination or a timeout >> /dev/stderr
    exit $EXIT_CODE
  fi

  OF=wdl_output.json;
  EP=.cause.stderr_file;
  if jq -re .error $OF 2> /dev/null; then
    if jq -re $EP $OF; then
      if tail -n 1 $(jq -r $EP $OF) | jq -re .wdl_error_message; then
        tail -n 1 $(jq -r $EP $OF) > $OF;
      else
        export err_type=UncaughtError err_msg=$(tail -n 1 $(jq -r $EP $OF))
        jq -nc ".wdl_error_message=true | .error=env.err_type | .cause=env.err_msg" > $OF;
      fi;
    fi;
    $aws s3 cp $OF "$WDL_OUTPUT_URI";
  fi
  # Exit deterministically with the (possibly re-surfaced) code so a spot-reclaim re-map to 143 is
  # actually what Batch records as the attempt's container exit code.
  exit $EXIT_CODE
}

trap handle_error EXIT
miniwdl run $PASSTHRU_ARGS --dir $MINIWDL_DIR $(basename "$WDL_WORKFLOW_URI") --input wdl_input.json --verbose --error-json -o wdl_output.json
clean_wd
