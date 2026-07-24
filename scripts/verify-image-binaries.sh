#!/bin/bash
#
# verify-image-binaries.sh <image-reference> <platform> <binary-path>...
# verify-image-binaries.sh --self-test
#
# Second half of the multi-arch gate. verify-multiarch-image.sh proves the published index
# has a child whose CONFIG BLOB declares each required platform. This script proves the
# child actually CONTAINS binaries for that platform, by reading the ELF header of specific
# executables inside it.
#
# Both checks are needed, and the second is not paranoia. The swipe Dockerfile downloads
# architecture-specific binaries (s3parcp, docker-credential-ecr-login) keyed on
# ${TARGETARCH}. When TARGETARCH was declared as `ARG TARGETARCH=amd64`, the default
# SHADOWED the value BuildKit injects, so an arm64 build silently fetched the amd64
# binaries. The resulting image is a genuine arm64 image -- correct manifest, correct
# config blob, passes every architecture check that stops at metadata -- and dies at
# runtime with `exec format error`. The Dockerfile no longer does that, but a one-character
# edit can reintroduce it, and nothing above the file layer would notice.
#
# Nothing here executes the foreign-architecture binary: `docker create` (never `start`)
# materialises a container filesystem so `docker cp` can pull the file out, and the check
# is a read of the ELF e_machine field. That works on any runner, no QEMU.
#
# `--self-test` runs the ELF decoder against synthetic headers -- one x86-64, one AArch64,
# and each against the wrong expectation -- and fails unless it accepts the right ones and
# REJECTS the wrong ones. Run it before the real checks so a decoder that has degraded into
# an unconditional "OK" cannot ship green.

set -euo pipefail

# ELF e_machine values (2 bytes little-endian at offset 0x12) for the architectures we ship.
machine_for_platform() {
  case "$1" in
    linux/amd64) echo "62 x86-64" ;;
    linux/arm64) echo "183 AArch64" ;;
    *) return 1 ;;
  esac
}

# check_elf <file> <want_machine> <want_name> <label>
# Returns 0 only if <file> is an ELF binary whose e_machine equals <want_machine>.
check_elf() {
  local file="$1" want_machine="$2" want_name="$3" label="$4"

  local magic
  magic="$(od -An -tx1 -N4 "${file}" | tr -d ' \n')"
  if [ "${magic}" != "7f454c46" ]; then
    echo "  FAIL ${label}: not an ELF binary (magic ${magic})"
    return 1
  fi

  local b0 b1 machine
  read -r b0 b1 <<< "$(od -An -tu1 -j18 -N2 "${file}")"
  machine=$(( b0 + b1 * 256 ))
  if [ "${machine}" -ne "${want_machine}" ]; then
    echo "  FAIL ${label}: ELF e_machine=${machine}, expected ${want_machine} (${want_name})"
    return 1
  fi

  echo "  OK ${label}: ELF e_machine=${machine} (${want_name})"
  return 0
}

self_test() {
  local work
  work="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '${work}'" RETURN

  # 20-byte synthetic ELF64 little-endian headers differing only in e_machine.
  printf '\177ELF\002\001\001\000\000\000\000\000\000\000\000\000\002\000\076\000' > "${work}/x86.elf"
  printf '\177ELF\002\001\001\000\000\000\000\000\000\000\000\000\002\000\267\000' > "${work}/arm.elf"
  printf 'not an elf at all, just some bytes here' > "${work}/junk.bin"

  local rc=0
  echo "self-test: decoder must ACCEPT matching architectures"
  check_elf "${work}/x86.elf" 62 x86-64 "synthetic x86-64" || rc=1
  check_elf "${work}/arm.elf" 183 AArch64 "synthetic AArch64" || rc=1

  echo "self-test: decoder must REJECT mismatched architectures and non-ELF files"
  if check_elf "${work}/x86.elf" 183 AArch64 "synthetic x86-64 claimed as AArch64" > /dev/null 2>&1; then
    echo "  FAIL: decoder accepted an x86-64 binary as AArch64"
    rc=1
  else
    echo "  OK: rejected x86-64 claimed as AArch64"
  fi
  if check_elf "${work}/arm.elf" 62 x86-64 "synthetic AArch64 claimed as x86-64" > /dev/null 2>&1; then
    echo "  FAIL: decoder accepted an AArch64 binary as x86-64"
    rc=1
  else
    echo "  OK: rejected AArch64 claimed as x86-64"
  fi
  if check_elf "${work}/junk.bin" 62 x86-64 "non-ELF file" > /dev/null 2>&1; then
    echo "  FAIL: decoder accepted a non-ELF file"
    rc=1
  else
    echo "  OK: rejected non-ELF file"
  fi

  if [ "${rc}" -ne 0 ]; then
    echo "::error::verify-image-binaries.sh self-test FAILED: the ELF architecture check is not working, so its verdicts are meaningless"
    return 1
  fi
  echo "self-test passed: the ELF architecture check accepts matches and rejects mismatches"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then
  self_test
  exit $?
fi

IMAGE_REF="${1:?usage: verify-image-binaries.sh <image-reference> <platform> <binary-path>...}"
PLATFORM="${2:?usage: verify-image-binaries.sh <image-reference> <platform> <binary-path>...}"
shift 2
BINARIES=("$@")
if [ "${#BINARIES[@]}" -eq 0 ]; then
  echo "verify-image-binaries.sh: no binaries given" >&2
  exit 2
fi

if ! read -r want_machine want_name <<< "$(machine_for_platform "${PLATFORM}")"; then
  echo "verify-image-binaries.sh: unsupported platform ${PLATFORM}" >&2
  exit 2
fi

work="$(mktemp -d)"
cid=""
cleanup() {
  if [ -n "${cid}" ]; then docker rm -f "${cid}" > /dev/null 2>&1 || true; fi
  rm -rf "${work}"
}
trap cleanup EXIT

echo "verifying binaries in ${IMAGE_REF} (${PLATFORM}) are ${want_name}"

docker pull --quiet --platform "${PLATFORM}" "${IMAGE_REF}" > /dev/null
# `create`, never `start`: this only needs the filesystem, and the binaries are for another
# architecture on most runners.
cid="$(docker create --platform "${PLATFORM}" "${IMAGE_REF}" /bin/true)"

failed=0
for bin in "${BINARIES[@]}"; do
  out="${work}/$(basename "${bin}")"
  if ! docker cp "${cid}:${bin}" "${out}" > /dev/null 2>&1; then
    echo "::error::${IMAGE_REF} (${PLATFORM}) does not contain ${bin}"
    failed=1
    continue
  fi
  if ! check_elf "${out}" "${want_machine}" "${want_name}" "${bin}"; then
    echo "::error::${bin} in ${IMAGE_REF} is labelled ${PLATFORM} but is not a ${want_name} binary"
    failed=1
  fi
done

if [ "${failed}" -ne 0 ]; then
  echo "::error::${IMAGE_REF} (${PLATFORM}) failed binary architecture verification"
  exit 1
fi

echo "OK: every checked binary in ${IMAGE_REF} (${PLATFORM}) is ${want_name}"
