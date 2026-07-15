"""Shared fail-closed identity checks for the modern google/tcmalloc baseline."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

UPSTREAM_URL = "https://github.com/google/tcmalloc.git"
UPSTREAM_REVISION = "12f255231938d30493186b0a037feedd70f5a1c1"
UPSTREAM_DATE = "2025-09-26"
BAZEL_VERSION = "8.4.2"
LIBRARY_NAME = "libunialloc_google_tcmalloc.so"
PROVENANCE_NAME = "google-tcmalloc-provenance.json"
REVISION_SYMBOL = f"unialloc_google_tcmalloc_revision_{UPSTREAM_REVISION}"
REQUIRED_SYMBOLS = (
    "malloc",
    "free",
    "unialloc_google_tcmalloc_alloc",
    "unialloc_google_tcmalloc_dealloc",
    "unialloc_google_tcmalloc_revision",
    REVISION_SYMBOL,
    "unialloc_google_tcmalloc_hpaa_active",
    "unialloc_google_tcmalloc_malloc_provider_is_self",
)
REQUIRED_PROBE_LINES = (
    f"upstream_revision={UPSTREAM_REVISION}",
    "hpaa_stats_present=1",
    "hpaa_subrelease_enabled=1",
)
RUNTIME_IDENTITY_MARKER = "unialloc: verified modern google/tcmalloc HPAA identity\n"
RUNTIME_FAILURE_MARKER = "unialloc: modern google/tcmalloc HPAA identity check failed\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def library_path(lib_dir: Path) -> Path:
    return lib_dir / LIBRARY_NAME


def provenance_path(lib_dir: Path) -> Path:
    return lib_dir.parent / PROVENANCE_NAME


def parse_exported_dynamic_symbols(output: str) -> set[str]:
    exported: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].removesuffix(":").isdigit():
            continue
        _number, _value, _size, _kind, binding, visibility, section, name = fields[:8]
        if binding not in {"GLOBAL", "WEAK"} or visibility != "DEFAULT" or section == "UND":
            continue
        exported.add(name.split("@", 1)[0])
    return exported


def exported_dynamic_symbols(library: Path) -> set[str]:
    readelf = shutil.which("readelf")
    if readelf is None:
        raise RuntimeError("readelf is required for google/tcmalloc authentication")
    output = subprocess.run(
        [readelf, "--wide", "--dyn-syms", str(library)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout
    return parse_exported_dynamic_symbols(output)


def validate_library_dir(path: Path | None, *, inspect_symbols: bool = True) -> dict[str, Any]:
    if path is None:
        raise RuntimeError("a modern google/tcmalloc library directory is required")
    lib_dir = path.expanduser().resolve(strict=True)
    library = library_path(lib_dir)
    if not library.is_file():
        raise RuntimeError(f"modern google/tcmalloc artifact missing: {library}")
    manifest_path = provenance_path(lib_dir)
    if not manifest_path.is_file():
        raise RuntimeError(f"google/tcmalloc provenance missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    probe = manifest.get("hpaa_probe_output", [])
    if (
        manifest.get("allocator_family") != "google/tcmalloc"
        or manifest.get("upstream_revision") != UPSTREAM_REVISION
        or manifest.get("variant") != "modern-hpaa-adaptive-subrelease"
        or manifest.get("bazel_version") != BAZEL_VERSION
        or manifest.get("library_sha256") != sha256_file(library)
        or not set(REQUIRED_PROBE_LINES).issubset(set(probe))
    ):
        raise RuntimeError("google/tcmalloc provenance or HPAA proof is invalid")
    if inspect_symbols:
        exported = exported_dynamic_symbols(library)
        missing = [symbol for symbol in REQUIRED_SYMBOLS if symbol not in exported]
        if missing:
            raise RuntimeError(f"google/tcmalloc artifact is missing symbols: {missing}")
    return {
        "path": str(library),
        "realpath": str(library.resolve(strict=True)),
        "size_bytes": library.stat().st_size,
        "sha256": sha256_file(library),
        "allocator_family": manifest["allocator_family"],
        "variant": manifest["variant"],
        "upstream_revision": manifest["upstream_revision"],
        "bazel_version": manifest["bazel_version"],
        "hpaa_probe_output": probe,
        "provenance_path": str(manifest_path),
    }


def rust_allocator_adapter_source(
    *,
    feature: str,
    static_name: str,
    conflicting_features: tuple[str, ...] = (),
) -> str:
    """Return the revision-bound C-ABI adapter used by external Rust crates.

    The generated source deliberately stays within the Rust 1.54 language and
    library surface used by the paper-era nightly checkouts.  Its unique bridge
    references keep the shared library on the link line, while the executable
    constructor independently rejects a wrong revision, an inactive HPAA page
    heap, or a foreign malloc provider before `main` runs.  The authenticated
    DSO constructor emits the single process identity marker.
    """

    if not feature.replace("_", "").isalnum():
        raise ValueError(f"invalid Cargo feature name: {feature!r}")
    if not static_name.replace("_", "").isalnum():
        raise ValueError(f"invalid Rust static name: {static_name!r}")
    conflicts = [name for name in conflicting_features if name and name != feature]
    conflict_guard = ""
    if conflicts:
        conflict_terms = ",\n        ".join(f'feature = "{name}"' for name in conflicts)
        conflict_guard = f'''\n#[cfg(all(\n    feature = "{feature}",\n    any(\n        {conflict_terms}\n    )\n))]\ncompile_error!("{feature} must be the only enabled benchmark allocator feature");\n'''

    return f'''#[cfg(all(feature = "{feature}", not(target_os = "linux")))]
compile_error!("{feature} requires the pinned Linux google/tcmalloc artifact");
{conflict_guard}
#[cfg(feature = "{feature}")]
#[global_allocator]
static {static_name}: std::alloc::System = std::alloc::System;

#[cfg(all(feature = "{feature}", target_os = "linux"))]
mod unialloc_google_tcmalloc_runtime_identity {{
    use std::os::raw::{{c_char, c_int, c_void}};

    const EXPECTED_REVISION: &[u8] = b"{UPSTREAM_REVISION}\\0";
    const FAILURE: &[u8] = b"{RUNTIME_FAILURE_MARKER.rstrip()}\\n";

    #[link(name = "unialloc_google_tcmalloc")]
    extern "C" {{
        fn unialloc_google_tcmalloc_revision() -> *const c_char;
        fn unialloc_google_tcmalloc_hpaa_active() -> c_int;
        fn unialloc_google_tcmalloc_malloc_provider_is_self() -> c_int;
        fn {REVISION_SYMBOL}();
        fn write(fd: c_int, buffer: *const c_void, count: usize) -> isize;
        fn _exit(status: c_int) -> !;
    }}

    unsafe fn revision_matches(mut actual: *const c_char) -> bool {{
        if actual.is_null() {{
            return false;
        }}
        for expected in EXPECTED_REVISION.iter() {{
            if *(actual as *const u8) != *expected {{
                return false;
            }}
            if *expected == 0 {{
                return true;
            }}
            actual = actual.add(1);
        }}
        false
    }}

    unsafe fn emit(message: &[u8]) {{
        write(2, message.as_ptr() as *const c_void, message.len());
    }}

    unsafe extern "C" fn verify_google_tcmalloc() {{
        // This revision-named reference makes gperftools fail at link time.
        {REVISION_SYMBOL}();
        if !revision_matches(unialloc_google_tcmalloc_revision())
            || unialloc_google_tcmalloc_hpaa_active() != 1
            || unialloc_google_tcmalloc_malloc_provider_is_self() != 1
        {{
            emit(FAILURE);
            _exit(86);
        }}
    }}

    #[used]
    #[link_section = ".init_array"]
    static VERIFY_GOOGLE_TCMALLOC: unsafe extern "C" fn() = verify_google_tcmalloc;
}}
'''
