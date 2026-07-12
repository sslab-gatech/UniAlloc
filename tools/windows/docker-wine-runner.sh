#!/usr/bin/env bash
set -euo pipefail

# Execute a Windows .exe produced by UniAlloc's x86_64-pc-windows-gnu build
# inside the pinned Docker/Wine runner.  This gives non-Windows hosts a real
# userspace execution path for platform_allocator_workload evidence without
# pretending that a cargo-check-only artifact is claim-grade platform support.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <windows-exe-under-repo> [args...]" >&2
  exit 64
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to run the Windows/Wine platform workload runner" >&2
  exit 127
fi

HOST_EXE="$1"
shift

case "$HOST_EXE" in
  /*) ABS_EXE="$HOST_EXE" ;;
  *) ABS_EXE="$REPO_ROOT/$HOST_EXE" ;;
esac

# Normalize path without requiring GNU realpath on macOS.
if [[ -e "$ABS_EXE" ]]; then
  ABS_EXE="$(cd "$(dirname "$ABS_EXE")" && pwd)/$(basename "$ABS_EXE")"
fi

if [[ ! -f "$ABS_EXE" ]]; then
  echo "Windows executable does not exist: $ABS_EXE" >&2
  exit 66
fi

case "$ABS_EXE" in
  "$REPO_ROOT"/*) REL_EXE="${ABS_EXE#$REPO_ROOT/}" ;;
  *)
    echo "Windows executable must be inside the UniAlloc repository so Docker can mount it read-only: $ABS_EXE" >&2
    exit 65
    ;;
esac

IMAGE="${UNIALLOC_WINDOWS_WINE_IMAGE:-unialloc-windows-wine-runner:trixie}"
PLATFORM="${UNIALLOC_WINDOWS_WINE_PLATFORM:-linux/amd64}"
WINEPREFIX_VOLUME="${UNIALLOC_WINDOWS_WINEPREFIX_VOLUME:-unialloc-windows-wine-prefix-trixie}"
WINE_ARCH="${UNIALLOC_WINDOWS_WINEARCH:-win64}"
CONTAINER_EXE="Z:\\work\\${REL_EXE//\//\\}"

# Keep the repo mount read-only: the runner is evidence collection, not a build
# step.  Persist WINEPREFIX in a named volume so first-run Wine setup is not
# paid on every sample and remains isolated from the developer host.
exec docker run --rm \
  --platform "$PLATFORM" \
  -e WINEDEBUG="${WINEDEBUG:--all}" \
  -e WINEARCH="$WINE_ARCH" \
  -e WINEPREFIX=/wineprefix \
  -v "$REPO_ROOT:/work:ro" \
  -v "$WINEPREFIX_VOLUME:/wineprefix" \
  -w /work \
  "$IMAGE" \
  "$CONTAINER_EXE" \
  "$@"
