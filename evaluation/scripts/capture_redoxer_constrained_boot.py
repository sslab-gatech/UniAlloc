#!/usr/bin/env python3
"""Capture a real Docker/redoxer UniAlloc Redox boot transcript with provenance.

The ordinary `redoxer exec` path creates a temporary `redoxer.bin` disk image and
then deletes it.  C007 cannot use the target transcript unless that image and the
boot configuration are preserved and hash-bound to the transcript.  This helper
runs the real Docker/redoxer build+exec path, copies the temporary disk image
while QEMU is still running, writes a boot-config JSON file, and appends a
`UNIALLOC_CONSTRAINED_BOOT_PROVENANCE` marker to the preserved transcript.

It intentionally does not import the platform matrix.  Validate/import with:

  python3 evaluation/scripts/evaluate.py validate-constrained-boot-log ...
  python3 evaluation/scripts/evaluate.py collect-platform-smoke --platforms redox ...

The helper keeps the Docker redoxer target under one run directory and removes
large temporary build directories by default, leaving only the preserved image,
logs, boot config, binary, and summary.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW = ROOT / "evaluation" / "raw"
MARKER = "UNIALLOC_CONSTRAINED_BOOT_PROVENANCE"
SAMPLE_MARKER = "UNIALLOC_CONSTRAINED_BOOT_SAMPLE"


def now_iso() -> str:
    return _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def marker_token(value: str) -> str:
    out = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_.:") else "_" for ch in value.strip())
    return out or "unknown"


def rel_to_root(path: pathlib.Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def docker_image_version(image: str) -> str:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}} {{json .RepoDigests}}"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except Exception as exc:  # pragma: no cover - best-effort diagnostic
        return f"{image} inspect-error:{type(exc).__name__}:{exc}"
    text = " ".join(part.strip() for part in (proc.stdout, proc.stderr) if part.strip())
    return text or image


def docker_exec_copy_image(container: str, tmp_inside: str, dest_inside: str) -> Tuple[bool, str]:
    """Copy redoxer's temp disk inside the running container with sparse support."""

    script = f'''
set -eu
src="$(find {sh_quote(tmp_inside)} -path '*/redoxer.bin' -type f -print -quit)"
if [ -z "$src" ]; then
  echo "redoxer.bin not found under {tmp_inside}" >&2
  exit 2
fi
mkdir -p "$(dirname {sh_quote(dest_inside)})"
rm -f {sh_quote(dest_inside)}.tmp
if ! cp --sparse=always "$src" {sh_quote(dest_inside)}.tmp; then
  echo "sparse copy failed; retrying with ordinary cp" >&2
  rm -f {sh_quote(dest_inside)}.tmp
  cp "$src" {sh_quote(dest_inside)}.tmp
fi
sync {sh_quote(dest_inside)}.tmp 2>/dev/null || true
mv {sh_quote(dest_inside)}.tmp {sh_quote(dest_inside)}
sha256sum {sh_quote(dest_inside)}
du -h {sh_quote(dest_inside)} 2>/dev/null || true
'''
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", script],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    return proc.returncode == 0, "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def remove_dir(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "existed": False, "removed": False, "removed_bytes": 0}
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (pathlib.Path(root) / name).stat().st_size
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    return {"path": str(path), "existed": True, "removed": not path.exists(), "removed_bytes": total}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", default="")
    p.add_argument("--output-dir", help="Default: evaluation/raw/<run-id>")
    p.add_argument("--docker-image", default="redoxos/redoxer:latest")
    p.add_argument("--docker-platform", default="linux/amd64")
    p.add_argument("--target", default="x86_64-unknown-redox")
    p.add_argument("--features", default="fixed_heap,allow_mem_leak,stats")
    p.add_argument("--timeout", type=int, default=420)
    p.add_argument("--keep-target-dirs", action="store_true", help="Keep redoxer target/tmp dirs for debugging")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not shutil.which("docker"):
        print("docker executable is required", file=sys.stderr)
        return 127

    run_id = args.run_id or _dt.datetime.utcnow().strftime("c007-redox-docker-redoxer-provenance-%Y%m%d-%H%M%S")
    out_dir = pathlib.Path(args.output_dir).expanduser() if args.output_dir else RAW / run_id
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rel_out = rel_to_root(out_dir)
    except ValueError:
        print(f"output dir must be under repo root for Docker bind mount: {out_dir}", file=sys.stderr)
        return 2

    image_path = out_dir / "redoxer-preserved-image.bin"
    raw_transcript = out_dir / "redoxer-small-heap.raw.log"
    transcript = out_dir / "redoxer-small-heap.provenance.log"
    build_log = out_dir / "redoxer-build.log"
    boot_config = out_dir / "redoxer-boot-config.json"
    binary_path = out_dir / "redox-root" / "small_heap"
    target_dir = out_dir / "redoxer-target"
    tmp_dir = out_dir / "redoxer-tmp"
    inner_script = out_dir / "redoxer-inner-run.sh"
    image_inside = f"/work/{rel_out}/redoxer-preserved-image.bin"
    tmp_inside = f"/work/{rel_out}/redoxer-tmp"

    docker_version = docker_image_version(args.docker_image)
    boot_config_record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "capture-redoxer-constrained-boot",
        "generated_at": now_iso(),
        "docker_image": args.docker_image,
        "docker_platform": args.docker_platform,
        "docker_image_version": docker_version,
        "target": args.target,
        "features": args.features,
        "command": "redoxer build ... && redoxer exec -o - -f <redox-root:/root> /root/redox-root/small_heap",
        "binary_path": str(binary_path),
        "preserved_image_path": str(image_path),
        "notes": "Boot config for real Docker/redoxer Redox/QEMU UniAlloc small_heap run; hashed into provenance marker.",
    }
    write_json(boot_config, boot_config_record)

    inner_script.write_text(
        f"""#!/usr/bin/env sh
set -eu
cd /work
RAW=/work/{rel_out}
export TMPDIR="$RAW/redoxer-tmp"
export CARGO_TARGET_DIR="$RAW/redoxer-target"
mkdir -p "$TMPDIR" "$RAW/redox-root"
redoxer build -p unialloc --example small_heap --target {args.target} \
  --no-default-features --features {sh_quote(args.features)} > "$RAW/redoxer-build.log" 2>&1
BIN="$CARGO_TARGET_DIR/{args.target}/debug/examples/small_heap"
cp "$BIN" "$RAW/redox-root/small_heap"
chmod +x "$RAW/redox-root/small_heap"
sha256sum "$RAW/redox-root/small_heap" > "$RAW/small_heap-redox.sha256"
exec redoxer exec -o - -f "$RAW/redox-root:/root" /root/redox-root/small_heap
""",
        encoding="utf-8",
    )
    inner_script.chmod(0o755)

    container = f"unialloc-redoxer-{os.getpid()}-{int(time.time())}"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container,
        "--platform",
        args.docker_platform,
        "-v",
        f"{ROOT}:/work",
        "-w",
        "/work",
        args.docker_image,
        "sh",
        f"/work/{rel_out}/redoxer-inner-run.sh",
    ]

    start = time.monotonic()
    copied = False
    copy_log = ""
    timed_out = False
    lines: List[str] = []
    proc = subprocess.Popen(
        docker_cmd,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            lines.append(line)
            raw_transcript.write_text("".join(lines), encoding="utf-8", errors="replace")
            if not copied and ("Installing to RedoxFS partition" in line or "## redoxer" in line):
                ok, detail = docker_exec_copy_image(container, tmp_inside, image_inside)
                copy_log += detail
                copied = ok and image_path.exists()
            if time.monotonic() - start > args.timeout:
                timed_out = True
                subprocess.run(["docker", "kill", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
        returncode = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["docker", "kill", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        returncode = proc.wait(timeout=30)

    raw_text = "".join(lines)
    raw_transcript.write_text(raw_text, encoding="utf-8", errors="replace")
    if not copied and not timed_out:
        # Last-chance copy while --rm cleanup may still be pending; usually this is too late,
        # but it keeps the command honest if the image remained visible.
        try:
            ok, detail = docker_exec_copy_image(container, tmp_inside, image_inside)
            copy_log += detail
            copied = ok and image_path.exists()
        except Exception as exc:
            copy_log += f"\nlast-chance copy failed: {type(exc).__name__}: {exc}\n"

    blockers: List[str] = []
    if timed_out:
        blockers.append(f"docker/redoxer command timed out after {args.timeout}s")
    if returncode != 0:
        blockers.append(f"docker/redoxer command exited {returncode}")
    if SAMPLE_MARKER not in raw_text:
        blockers.append(f"redoxer transcript did not contain {SAMPLE_MARKER}")
    if "## redoxer (success) ##" not in raw_text:
        blockers.append("redoxer transcript did not contain the success marker")
    if not image_path.exists():
        blockers.append("redoxer temporary disk image was not preserved")
    if not binary_path.exists():
        blockers.append("Redox target small_heap binary was not preserved")
    if not boot_config.exists():
        blockers.append("boot config JSON was not written")

    image_sha = sha256_file(image_path) if image_path.exists() else None
    boot_config_sha = sha256_file(boot_config) if boot_config.exists() else None
    binary_sha = sha256_file(binary_path) if binary_path.exists() else None
    marker = None
    if not blockers and image_sha and boot_config_sha:
        marker = (
            f"{MARKER} platform=redox image_sha256={image_sha} "
            f"boot_config_sha256={boot_config_sha} "
            f"emulator={marker_token('docker://' + args.docker_image)} "
            f"emulator_version={marker_token(docker_version)}"
        )
        transcript.write_text(raw_text.rstrip() + "\n" + marker + "\n", encoding="utf-8")
    else:
        transcript.write_text(raw_text, encoding="utf-8")

    cleanup = {}
    if not args.keep_target_dirs:
        cleanup["redoxer_target"] = remove_dir(target_dir)
        cleanup["redoxer_tmp"] = remove_dir(tmp_dir)

    summary = {
        "schema_version": 1,
        "source": "capture-redoxer-constrained-boot",
        "generated_at": now_iso(),
        "ready_for_validate_constrained_boot_log": not blockers,
        "blockers": blockers,
        "returncode": returncode,
        "timed_out": timed_out,
        "docker_command": docker_cmd,
        "docker_image": args.docker_image,
        "docker_platform": args.docker_platform,
        "docker_image_version": docker_version,
        "copy_log": copy_log,
        "raw_transcript": str(raw_transcript),
        "provenance_transcript": str(transcript),
        "build_log": str(build_log),
        "boot_config": str(boot_config),
        "preserved_image": str(image_path) if image_path.exists() else None,
        "preserved_image_sha256": image_sha,
        "preserved_image_size": image_path.stat().st_size if image_path.exists() else None,
        "binary": str(binary_path) if binary_path.exists() else None,
        "binary_sha256": binary_sha,
        "provenance_marker": marker,
        "cleanup": cleanup,
        "validation_command": [
            "python3",
            "evaluation/scripts/evaluate.py",
            "validate-constrained-boot-log",
            "--platform",
            "redox",
            "--boot-log",
            str(transcript),
            "--image",
            str(image_path),
            "--emulator",
            f"docker://{args.docker_image}",
            "--boot-config",
            str(boot_config),
            "--build-log",
            str(build_log),
            "--boot-success-pattern",
            SAMPLE_MARKER,
        ],
    }
    summary_path = out_dir / "redoxer-capture-summary.json"
    write_json(summary_path, summary)
    print(json.dumps({
        "summary": str(summary_path),
        "ready_for_validate_constrained_boot_log": summary["ready_for_validate_constrained_boot_log"],
        "blockers": blockers,
        "transcript": str(transcript),
        "image": str(image_path) if image_path.exists() else None,
        "boot_config": str(boot_config),
        "build_log": str(build_log),
        "image_sha256": image_sha,
    }, indent=2, sort_keys=True))
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
