#!/bin/bash
#
# verify-multiarch-image.sh <ecr-repository> <image-reference> <required-platform>...
#
# Hard gate for the swipe image publish path. Exits non-zero unless <image-reference>
# resolves to a manifest index whose children GENUINELY are every required platform.
#
# Why this is not just `docker manifest inspect | grep arm64`:
#
#   A manifest index carries a `.manifests[].platform` object. That object is metadata
#   the pusher writes; nothing in the registry validates it against the image it points
#   at. `docker buildx imagetools create` will happily label an amd64 child as arm64 if
#   told to. The authoritative statement of an image's architecture is the `architecture`
#   and `os` fields inside its CONFIG BLOB -- the same document the container runtime
#   itself reads, and the document whose digest is content-addressed by the child
#   manifest. So this script walks:
#
#       tag -> index -> child manifest -> config blob digest -> config blob -> .architecture/.os
#
#   and compares the config blob's own claim, not the index's claim about it. It ALSO
#   requires the index's declared platform to agree with the config blob, so a mislabelled
#   index is a failure rather than something that quietly resolves to the wrong image at
#   `docker pull` time.
#
# The cost of not doing this is not theoretical: swipe:v1.4.9-seqtoid.1-arm64 was published
# by hand as an arm64-only image, and the x86_64 Batch compute environments that pull it
# failed every user workflow with CannotPullImageManifestError.
#
# Blob reads go through the ECR API (batch-get-image + get-download-url-for-layer) rather
# than a registry client, because that is the one path that is unambiguously fetching the
# blob bytes and not re-reading cached index metadata.

set -euo pipefail

ECR_REPO="${1:?usage: verify-multiarch-image.sh <ecr-repository> <image-reference> <platform>...}"
IMAGE_REF="${2:?usage: verify-multiarch-image.sh <ecr-repository> <image-reference> <platform>...}"
shift 2
REQUIRED_PLATFORMS=("$@")
if [ "${#REQUIRED_PLATFORMS[@]}" -eq 0 ]; then
  echo "verify-multiarch-image.sh: no required platforms given" >&2
  exit 2
fi

MEDIA_TYPES="application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.index.v1+json"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

fail() {
  echo "VERIFY FAIL: $*" >&2
}

# Resolve <image-reference> to the set of "os/architecture" strings its children's CONFIG
# BLOBS declare. Prints one platform per line on stdout. Returns non-zero if the reference
# is not an index, if a child cannot be resolved, or if a child's index-declared platform
# disagrees with its config blob.
resolve_config_platforms() {
  local ref="$1"
  local index_json="${work}/index.json"

  docker buildx imagetools inspect --raw "${ref}" > "${index_json}"

  local media_type
  media_type="$(jq -r '.mediaType // ""' "${index_json}")"
  case "${media_type}" in
    application/vnd.oci.image.index.v1+json | application/vnd.docker.distribution.manifest.list.v2+json) ;;
    *)
      fail "${ref} is not a manifest index (mediaType=${media_type:-<absent>}); a single-architecture image cannot satisfy a multi-arch requirement"
      return 1
      ;;
  esac

  # Attestation manifests (buildx provenance/sbom) carry platform.architecture == "unknown";
  # drop them so they cannot be mistaken for a real architecture entry either way.
  local children
  children="$(jq -r '
    [ .manifests[]
      | select((.platform.architecture // "unknown") != "unknown")
      | "\(.digest) \(.platform.os)/\(.platform.architecture)" ]
    | .[]' "${index_json}")"

  if [ -z "${children}" ]; then
    fail "${ref} index has no non-attestation children"
    return 1
  fi

  local digest declared
  while read -r digest declared; do
    [ -n "${digest}" ] || continue

    # child manifest -> config blob digest
    local child_manifest="${work}/child.json"
    aws ecr batch-get-image \
      --repository-name "${ECR_REPO}" \
      --image-ids "imageDigest=${digest}" \
      --accepted-media-types "${MEDIA_TYPES}" \
      --output json | jq -r '.images[0].imageManifest // ""' > "${child_manifest}"

    if [ ! -s "${child_manifest}" ]; then
      fail "child ${digest} of ${ref} could not be fetched from ECR repository ${ECR_REPO}"
      return 1
    fi

    local config_digest
    config_digest="$(jq -r '.config.digest // ""' "${child_manifest}")"
    if [ -z "${config_digest}" ]; then
      fail "child ${digest} of ${ref} has no config descriptor"
      return 1
    fi

    # config blob digest -> the blob itself. This is the byte-for-byte document the
    # container runtime reads to decide whether it can run the image.
    local url
    url="$(aws ecr get-download-url-for-layer \
      --repository-name "${ECR_REPO}" \
      --layer-digest "${config_digest}" \
      --query 'downloadUrl' --output text)"

    local config_blob="${work}/config.json"
    curl -sSfL "${url}" > "${config_blob}"

    # Verify we actually got the blob we asked for before believing anything in it.
    local actual_digest
    actual_digest="sha256:$(sha256sum "${config_blob}" | cut -d' ' -f1)"
    if [ "${actual_digest}" != "${config_digest}" ]; then
      fail "config blob for child ${digest} hashes to ${actual_digest}, expected ${config_digest}"
      return 1
    fi

    local blob_platform
    blob_platform="$(jq -r '"\(.os)/\(.architecture)"' "${config_blob}")"

    if [ "${blob_platform}" != "${declared}" ]; then
      fail "child ${digest} of ${ref} is declared ${declared} by the index but its config blob says ${blob_platform}"
      return 1
    fi

    echo "${blob_platform}"
  done <<< "${children}"
}

echo "verifying ${IMAGE_REF} carries: ${REQUIRED_PLATFORMS[*]}"

if ! found="$(resolve_config_platforms "${IMAGE_REF}")"; then
  echo "::error::${IMAGE_REF} failed multi-architecture verification"
  exit 1
fi

echo "config-blob platforms found in ${IMAGE_REF}:"
while IFS= read -r line; do echo "  ${line}"; done <<< "${found}"

missing=0
for want in "${REQUIRED_PLATFORMS[@]}"; do
  if ! grep -qx -- "${want}" <<< "${found}"; then
    echo "::error::${IMAGE_REF} has no child whose config blob declares ${want}"
    missing=1
  fi
done

if [ "${missing}" -ne 0 ]; then
  echo "::error::${IMAGE_REF} is not a genuine multi-architecture image; refusing to publish it"
  exit 1
fi

echo "OK: ${IMAGE_REF} genuinely carries ${REQUIRED_PLATFORMS[*]} per each child's config blob"
