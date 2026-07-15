#!/usr/bin/env python3
"""Build the pinned modern google/tcmalloc HPAA baseline for UniAlloc.

The produced DSO is a link-time artifact built from the same pinned source as
TCMalloc.  It is not a stable upstream ABI and must be rebuilt when the pin or
Bazel toolchain changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import google_tcmalloc_support as support

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_URL = support.UPSTREAM_URL
UPSTREAM_REVISION = support.UPSTREAM_REVISION
UPSTREAM_DATE = support.UPSTREAM_DATE
BAZEL_VERSION = support.BAZEL_VERSION
LOCKFILE = ROOT / "evaluation/config/google_tcmalloc_MODULE.bazel.lock"
LOCKFILE_SHA256 = "42848061424c8601d033e55157d9d1114c980343347b56e2af7bc6ac100702b0"
LIBRARY_NAME = support.LIBRARY_NAME
REVISION_SYMBOL = support.REVISION_SYMBOL
REQUIRED_SYMBOLS = support.REQUIRED_SYMBOLS
LIVE_GATE = ROOT / "evaluation/scripts/test_google_tcmalloc_live_integration.py"
LIVE_PREFIX_ENV = "UNIALLOC_GOOGLE_TCMALLOC_PREFIX"
LIVE_REQUIRE_ENV = "UNIALLOC_REQUIRE_LIVE_GOOGLE_TCMALLOC"
REQUIRED_LIVE_GATE_TESTS = (
    "test_live_artifact_passes_full_authentication",
    "test_preloaded_process_proves_runtime_identity",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.stdout


def parse_bazel_version(output: str) -> str:
    match = re.fullmatch(r"bazel\s+([0-9]+(?:\.[0-9]+){2})", output.strip())
    if match is None:
        raise RuntimeError(f"unrecognized Bazel version output: {output!r}")
    return match.group(1)


def ensure_preload_safe_prefix(prefix: Path) -> None:
    value = str(prefix)
    if os.pathsep in value or any(char.isspace() for char in value):
        raise RuntimeError(
            "google/tcmalloc prefix cannot contain LD_PRELOAD separators"
        )


def verify_live_artifact(prefix: Path) -> str:
    env = os.environ.copy()
    env[LIVE_PREFIX_ENV] = str(prefix)
    env[LIVE_REQUIRE_ENV] = "1"
    result = subprocess.run(
        [sys.executable, str(LIVE_GATE), "-v"],
        cwd=ROOT,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if "skipped=" in result.stdout:
        raise RuntimeError("required live Google TCMalloc gate reported a skip")
    missing_tests = [
        name for name in REQUIRED_LIVE_GATE_TESTS if name not in result.stdout
    ]
    if missing_tests:
        raise RuntimeError(
            f"required live Google TCMalloc tests did not execute: {missing_tests}"
        )
    return result.stdout


def ensure_source(source: Path) -> None:
    if not (source / ".git").exists():
        source.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q"], cwd=source)
        run(["git", "remote", "add", "origin", UPSTREAM_URL], cwd=source)
    remote = run(["git", "remote", "get-url", "origin"], cwd=source).strip()
    if remote != UPSTREAM_URL:
        raise RuntimeError(f"unexpected google/tcmalloc remote: {remote}")
    try:
        current = run(["git", "rev-parse", "HEAD"], cwd=source).strip()
    except subprocess.CalledProcessError:
        current = ""
    if current != UPSTREAM_REVISION:
        run(
            [
                "git",
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                "origin",
                UPSTREAM_REVISION,
            ],
            cwd=source,
        )
        run(["git", "checkout", "--detach", "--force", "FETCH_HEAD"], cwd=source)
    actual = run(["git", "rev-parse", "HEAD"], cwd=source).strip()
    if actual != UPSTREAM_REVISION:
        raise RuntimeError(f"source revision mismatch: {actual}")
    # A cached checkout is an input, not a trust root.  Remove tracked and
    # untracked modifications before materializing our generated integration.
    run(["git", "reset", "--hard", UPSTREAM_REVISION], cwd=source)
    run(["git", "clean", "-ffdx"], cwd=source)
    dirty = run(["git", "status", "--porcelain", "--ignored=no"], cwd=source).strip()
    if dirty:
        raise RuntimeError(f"google/tcmalloc source remains dirty: {dirty}")
    run(["git", "diff", "--quiet"], cwd=source)
    run(["git", "diff", "--cached", "--quiet"], cwd=source)


def integration_files() -> dict[str, str]:
    build = '''load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@rules_cc//cc:cc_library.bzl", "cc_library")
load("@rules_cc//cc:cc_shared_library.bzl", "cc_shared_library")

cc_library(
    name = "unialloc_shim",
    srcs = ["shim.cc"],
    deps = ["//tcmalloc:malloc_extension"],
)

cc_shared_library(
    name = "unialloc_google_tcmalloc",
    deps = [
        ":unialloc_shim",
        "//tcmalloc",
    ],
)

cc_binary(
    name = "hpaa_probe",
    srcs = ["hpaa_probe.cc"],
    malloc = "//tcmalloc",
    deps = ["//tcmalloc:malloc_extension"],
)
'''
    shim = f'''#include <cstddef>
#include <cstdlib>
#include <dlfcn.h>
#include <string>
#include <unistd.h>
#include "tcmalloc/malloc_extension.h"

extern "C" void* unialloc_google_tcmalloc_alloc(std::size_t size,
                                                  std::size_t alignment) noexcept {{
  if (alignment <= alignof(std::max_align_t)) return std::malloc(size);
  void* result = nullptr;
  return ::posix_memalign(&result, alignment, size) == 0 ? result : nullptr;
}}
extern "C" void unialloc_google_tcmalloc_dealloc(void* ptr) noexcept {{
  std::free(ptr);
}}
extern "C" const char* unialloc_google_tcmalloc_revision() noexcept {{
  return "{UPSTREAM_REVISION}";
}}
extern "C" void {REVISION_SYMBOL}() noexcept {{}}
extern "C" int unialloc_google_tcmalloc_hpaa_active() noexcept {{
  try {{
    const std::string stats = tcmalloc::MallocExtension::GetStats();
    return stats.find("HugePageAware:") != std::string::npos &&
           stats.find("PARAMETER hpaa_subrelease 1") != std::string::npos;
  }} catch (...) {{
    return 0;
  }}
}}
extern "C" int unialloc_google_tcmalloc_malloc_provider_is_self() noexcept {{
  void* resolved_malloc = ::dlsym(RTLD_DEFAULT, "malloc");
  void* resolved_free = ::dlsym(RTLD_DEFAULT, "free");
  if (resolved_malloc == nullptr || resolved_free == nullptr) {{
    return 0;
  }}
  using MallocFn = void* (*)(std::size_t);
  using FreeFn = void (*)(void*);
  auto malloc_fn = reinterpret_cast<MallocFn>(resolved_malloc);
  auto free_fn = reinterpret_cast<FreeFn>(resolved_free);
  void* allocation = malloc_fn(64);
  if (allocation == nullptr) {{
    return 0;
  }}
  const bool owned = tcmalloc::MallocExtension::GetOwnership(allocation) ==
                     tcmalloc::MallocExtension::Ownership::kOwned;
  free_fn(allocation);
  return owned ? 1 : 0;
}}
__attribute__((constructor(65535))) static void
unialloc_google_tcmalloc_verify_process_identity() noexcept {{
  static constexpr char kSuccess[] = "{support.RUNTIME_IDENTITY_MARKER.rstrip()}\\n";
  static constexpr char kFailure[] = "{support.RUNTIME_FAILURE_MARKER.rstrip()}\\n";
  if (unialloc_google_tcmalloc_hpaa_active() != 1 ||
      unialloc_google_tcmalloc_malloc_provider_is_self() != 1) {{
    ::write(STDERR_FILENO, kFailure, sizeof(kFailure) - 1);
    ::_exit(86);
  }}
  ::write(STDERR_FILENO, kSuccess, sizeof(kSuccess) - 1);
}}
'''
    probe = f'''#include <cstdlib>
#include <iostream>
#include <string>
#include "tcmalloc/malloc_extension.h"

int main() {{
  void* allocation = std::malloc(16 * 1024 * 1024);
  if (allocation == nullptr) return 2;
  static_cast<volatile char*>(allocation)[0] = 1;
  const std::string stats = tcmalloc::MallocExtension::GetStats();
  std::free(allocation);
  const bool hpaa = stats.find("HugePageAware:") != std::string::npos;
  const bool subrelease = stats.find("PARAMETER hpaa_subrelease 1") != std::string::npos;
  std::cout << "upstream_revision={UPSTREAM_REVISION}\\n";
  std::cout << "hpaa_stats_present=" << (hpaa ? 1 : 0) << "\\n";
  std::cout << "hpaa_subrelease_enabled=" << (subrelease ? 1 : 0) << "\\n";
  return hpaa && subrelease ? 0 : 3;
}}
'''
    return {"BUILD.bazel": build, "shim.cc": shim, "hpaa_probe.cc": probe}


def materialize_integration(source: Path) -> None:
    integration = source / "unialloc_integration"
    integration.mkdir(exist_ok=True)
    for name, content in integration_files().items():
        (integration / name).write_text(content, encoding="utf-8")
    if sha256_file(LOCKFILE) != LOCKFILE_SHA256:
        raise RuntimeError("pinned Bazel module lockfile hash mismatch")
    shutil.copy2(LOCKFILE, source / "MODULE.bazel.lock")


def build(args: argparse.Namespace) -> dict[str, object]:
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("modern google/tcmalloc baseline requires Linux x86_64/aarch64")
    ensure_preload_safe_prefix(args.prefix)
    source = args.cache_dir / f"google-tcmalloc-{UPSTREAM_REVISION}"
    ensure_source(source)
    materialize_integration(source)
    bazel = Path(args.bazel).expanduser().resolve(strict=True)
    env = os.environ.copy()
    env["USE_BAZEL_VERSION"] = BAZEL_VERSION
    version_result = subprocess.run(
        [str(bazel), "--version"],
        cwd=source,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    version_output = version_result.stdout.strip()
    actual_bazel_version = parse_bazel_version(version_output)
    if actual_bazel_version != BAZEL_VERSION:
        raise RuntimeError(
            f"google/tcmalloc pin requires Bazel {BAZEL_VERSION}, got {version_output}"
        )
    command = [
        str(bazel),
        "build",
        "//unialloc_integration:unialloc_google_tcmalloc",
        "//unialloc_integration:hpaa_probe",
        "--lockfile_mode=error",
        "--compilation_mode=opt",
        "--strip=always",
    ]
    build_output = run(command, cwd=source, env=env)
    # Bazel's workspace symlink tracks the just-built configuration.  Querying
    # `bazel info` without the opt flags would instead return a fastbuild path.
    bazel_bin = (source / "bazel-bin").resolve(strict=True)
    built_library = bazel_bin / "unialloc_integration" / LIBRARY_NAME
    probe = bazel_bin / "unialloc_integration" / "hpaa_probe"
    if not built_library.is_file() or not probe.is_file():
        raise RuntimeError("Bazel omitted the expected google/tcmalloc artifacts")
    probe_output = run([str(probe)], cwd=source)
    expected_probe = {
        f"upstream_revision={UPSTREAM_REVISION}",
        "hpaa_stats_present=1",
        "hpaa_subrelease_enabled=1",
    }
    if not expected_probe.issubset(set(probe_output.splitlines())):
        raise RuntimeError(f"HPAA probe failed: {probe_output!r}")
    exported = support.exported_dynamic_symbols(built_library)
    missing = [name for name in REQUIRED_SYMBOLS if name not in exported]
    if missing:
        raise RuntimeError(f"shared artifact is missing symbols: {missing}")

    lib_dir = args.prefix / "lib"
    installed = lib_dir / LIBRARY_NAME
    provenance = {
        "schema_version": 1,
        "allocator_family": "google/tcmalloc",
        "variant": "modern-hpaa-adaptive-subrelease",
        "upstream_url": UPSTREAM_URL,
        "upstream_revision": UPSTREAM_REVISION,
        "upstream_revision_date": UPSTREAM_DATE,
        "upstream_tree": run(
            ["git", "rev-parse", f"{UPSTREAM_REVISION}^{{tree}}"], cwd=source
        ).strip(),
        "pin_policy": "tested 2025 compatibility pin; upstream-current tracking is separate",
        "bazel_version": BAZEL_VERSION,
        "bazel_driver": str(bazel),
        "bazel_driver_sha256": sha256_file(bazel),
        "module_lock_sha256": sha256_file(LOCKFILE),
        "build_command": command,
        "build_output_sha256": hashlib.sha256(build_output.encode()).hexdigest(),
        "hpaa_probe_output": probe_output.splitlines(),
        "library": str(installed.resolve()),
        "canonical_prefix": str(args.prefix),
        "library_sha256": sha256_file(built_library),
        "required_symbols": list(REQUIRED_SYMBOLS),
    }

    # Prove the exact DSO in a disposable prefix before replacing a previously
    # valid installation.  A failed live gate therefore leaves the published
    # artifact untouched.
    with tempfile.TemporaryDirectory(
        prefix="unialloc-google-tcmalloc-live-"
    ) as staging_directory:
        staging_prefix = Path(staging_directory)
        staging_lib_dir = staging_prefix / "lib"
        staging_lib_dir.mkdir()
        staging_library = staging_lib_dir / LIBRARY_NAME
        shutil.copy2(built_library, staging_library)
        staging_library.chmod(0o755)
        staging_provenance = dict(provenance)
        staging_provenance["canonical_prefix"] = str(staging_prefix)
        staging_provenance["library"] = str(staging_library.resolve(strict=True))
        (staging_prefix / support.PROVENANCE_NAME).write_text(
            json.dumps(staging_provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        live_output = verify_live_artifact(staging_prefix)

    provenance["live_integration_gate"] = {
        "required": True,
        "test": str(LIVE_GATE.relative_to(ROOT)),
        "output_sha256": hashlib.sha256(live_output.encode()).hexdigest(),
    }

    lib_dir.mkdir(parents=True, exist_ok=True)
    staged = lib_dir / f".{LIBRARY_NAME}.tmp"
    staged.unlink(missing_ok=True)
    shutil.copy2(built_library, staged)
    staged.chmod(0o755)
    staged.replace(installed)
    # Never publish a generic libtcmalloc.so alias.  Old gperftools adapters
    # require different `tc_*` symbols and must stay on the explicit legacy path.
    (lib_dir / "libtcmalloc.so").unlink(missing_ok=True)
    provenance_path = args.prefix / "google-tcmalloc-provenance.json"
    staged_provenance_path = args.prefix / f".{support.PROVENANCE_NAME}.tmp"
    staged_provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    staged_provenance_path.replace(provenance_path)
    return provenance


def find_bazel() -> str | None:
    return shutil.which("bazelisk") or shutil.which("bazel")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache/unialloc/google-tcmalloc",
    )
    parser.add_argument(
        "--prefix", type=Path, default=ROOT / "evaluation/deps/tcmalloc"
    )
    parser.add_argument("--bazel", default=find_bazel())
    args = parser.parse_args(argv)
    if args.bazel is None:
        parser.error("Bazel/Bazelisk is required; pass --bazel PATH")
    args.cache_dir = args.cache_dir.expanduser().resolve()
    args.prefix = args.prefix.expanduser().resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    provenance = build(args)
    print(json.dumps(provenance, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
