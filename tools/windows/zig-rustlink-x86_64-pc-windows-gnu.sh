#!/usr/bin/env bash
set -euo pipefail

# Cargo's legacy x86_64-pc-windows-gnu target passes a MinGW-style link line
# that assumes x86_64-w64-mingw32-gcc and its import libraries are installed.
# Zig can provide the Windows GNU CRT/import libraries locally, but not with
# that exact legacy `-nodefaultlibs` link line.  This wrapper keeps Rust's
# object/rlib inputs and Windows system libraries, while removing MinGW CRT
# shims that Zig supplies itself.  It is for producing a real Windows .exe; the
# evaluator still requires a real Windows/Wine runner and valid workload JSON
# before accepting C007 Windows claim-grade evidence.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ -n "${UNIALLOC_ZIG:-}" ]]; then
  ZIG="$UNIALLOC_ZIG"
elif command -v zig >/dev/null 2>&1; then
  ZIG="$(command -v zig)"
elif [[ -x "$REPO_ROOT/.omx/tools/zig-aarch64-macos-0.16.0/zig" ]]; then
  ZIG="$REPO_ROOT/.omx/tools/zig-aarch64-macos-0.16.0/zig"
else
  echo "UNIALLOC_ZIG is not set and zig was not found on PATH or under .omx/tools" >&2
  exit 127
fi

args=()
for arg in "$@"; do
  case "$arg" in
    -nodefaultlibs|-lwinapi_*|-lmsvcrt|-lmingwex|-lmingw32|-l:libpthread.a)
      ;;
    *)
      args+=("$arg")
      ;;
  esac
done

exec "$ZIG" cc -target x86_64-windows-gnu "${args[@]}"
