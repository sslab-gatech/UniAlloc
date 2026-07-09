# C007 Redox boot evidence path

C007 only becomes claim-grade for Redox after a **real Redox target boot** (VM or
hardware) emits UniAlloc allocator evidence.  Host fixed-heap smoke output and
`cargo check --target x86_64-unknown-redox` are useful diagnostics, but they are
not boot evidence and must remain fail-closed.

The evaluator now also requires the Redox image and boot configuration to be
readable local files (or `file://` URIs) whenever a boot transcript is promoted
to claim-grade.  Their SHA-256 values are cross-checked against the
`UNIALLOC_CONSTRAINED_BOOT_PROVENANCE` marker, so a URI/label or copied marker
line is not enough.

## Required target transcript markers

A Redox boot log must contain both markers below:

1. `UNIALLOC_CONSTRAINED_BOOT_SAMPLE` from the Redox target runtime.  The sample
   must prove all of the following inside the target boot:
   - `platform=redox`
   - `fixed_heap_ready=true`
   - C ABI init/allocation/reallocation/deallocation ran
   - `c_abi_probe_passed=true`
   - positive typed allocation and deallocation counters
   - positive semantic type-stats rows with the probe row matched
2. `UNIALLOC_CONSTRAINED_BOOT_PROVENANCE` generated for the exact image, boot
   config, and emulator/runner used for the boot.  This binds the transcript to
   real artifacts by SHA-256 so a pasted success line cannot satisfy C007.

Generate the provenance marker after the Redox image/config exist:

```sh
python3 evaluation/scripts/evaluate.py emit-constrained-boot-provenance-marker \
  --platform redox \
  --image /path/to/redox.img \
  --boot-config /path/to/redox-boot.toml \
  --emulator /path/to/qemu-system-x86_64 \
  --output-dir evaluation/raw/redox-provenance-$(date +%Y%m%d)
```

The resulting marker must be printed into, or appended to, the captured Redox
boot transcript that also contains the target-emitted boot sample marker.

## Validate before import

Use the preflight validator first.  It preserves a hashed copy of the transcript
and refuses missing markers, host-smoke markers, mismatched image/config hashes,
and missing/false `c_abi_probe_passed`.

```sh
python3 evaluation/scripts/evaluate.py validate-constrained-boot-log \
  --platform redox \
  --boot-log /path/to/redox-boot.log \
  --image /path/to/redox.img \
  --boot-config /path/to/redox-boot.toml \
  --emulator /path/to/qemu-system-x86_64 \
  --boot-success-pattern UNIALLOC_BOOT_OK \
  --build-log /path/to/redox-build.log \
  --matrix-out evaluation/raw/redox-platform-matrix.generated.json
```

For a full platform collector refresh, pass the same real boot artifacts to
`collect-platform-smoke`:

```sh
python3 evaluation/scripts/evaluate.py collect-platform-smoke \
  --platforms redox \
  --rust-toolchain nightly \
  --redox-image /path/to/redox.img \
  --redox-boot-config /path/to/redox-boot.toml \
  --redox-emulator /path/to/qemu-system-x86_64 \
  --redox-boot-log /path/to/redox-boot.log \
  --redox-boot-success-pattern UNIALLOC_BOOT_OK
```

`--rust-toolchain` only overrides the evaluator process; it does not rewrite the
repository `rust-toolchain` pin.  Use it when the Redox target/sysroot requires a
newer rustc.

Only when `ready_for_platform_import=true` should the generated matrix be fed to
`import-platform-matrix` or to `collect-platform-smoke` with the same Redox boot
log metadata.

## Real redoxer/QEMU target-runtime capture

When local `qemu-system-x86_64`/`redoxer` are not installed but Docker is
available, the official `redoxos/redoxer` image can still run the UniAlloc Redox
binary inside Redox/QEMU. Keep the copied folder tiny; do **not** let redoxer
copy the repository root into the temporary Redox disk.

```sh
RAW=evaluation/raw/c007-redox-docker-redoxer-small-heap-$(date +%Y%m%d)
mkdir -p "$RAW/redox-root"

docker run --rm -v "$PWD":/work -w /work \
  -e CARGO_TARGET_DIR="/work/$RAW/redoxer-target" \
  redoxos/redoxer \
  redoxer build -p unialloc --example small_heap \
    --no-default-features --features fixed_heap,allow_mem_leak,stats

cp "$RAW/redoxer-target/x86_64-unknown-redox/debug/examples/small_heap" \
  "$RAW/redox-root/small_heap"

docker run --rm -v "$PWD":/work -w /work redoxos/redoxer \
  redoxer exec -o - --folder "/work/$RAW/redox-root:/root" \
  -- /root/redox-root/small_heap \
  2>&1 | tee "$RAW/redoxer-small-heap.log"

python3 evaluation/scripts/evaluate.py audit-redoxer-target-runtime \
  --log "$RAW/redoxer-small-heap.log" \
  --binary "$RAW/redox-root/small_heap" \
  --runner-image redoxos/redoxer:latest \
  --success-pattern UNIALLOC_CONSTRAINED_BOOT_SAMPLE \
  --output-dir "$RAW"
```

`redoxer exec --folder host_dir:/root` copies `host_dir` as
`/root/<basename(host_dir)>`; execute `/root/<basename>/small_heap` (or use an
equivalent staging layout).  The audit command checks the redoxer success marker,
the UniAlloc constrained boot sample, the trailing `{"passed":true,...}` JSON,
and the executed binary's SHA-256.  It deliberately writes
`claim_grade=false`: this proves target runtime behavior, but it is still not a
locally hash-bound Redox image/config boot transcript.

For claim-grade import, use the repository capture helper instead of only the
runtime audit:

```sh
python3 evaluation/scripts/capture_redoxer_constrained_boot.py \
  --run-id c007-redox-docker-redoxer-provenance-$(date +%Y%m%d) \
  --timeout 240
```

That helper stores the target transcript, copied Redox binary, build log,
boot-config JSON, and a sparse preserved Redox image under `evaluation/raw/`.
It then appends a provenance marker whose image/config hashes are verifiable by:

```sh
python3 evaluation/scripts/evaluate.py validate-constrained-boot-log \
  --platform redox \
  --boot-log evaluation/raw/<run-id>/redoxer-small-heap.provenance.log \
  --image evaluation/raw/<run-id>/redoxer-preserved-image.bin \
  --emulator docker://redoxos/redoxer:latest \
  --boot-config evaluation/raw/<run-id>/redoxer-boot-config.json \
  --build-log evaluation/raw/<run-id>/redoxer-build.log \
  --boot-success-pattern UNIALLOC_CONSTRAINED_BOOT_SAMPLE
```

For a repeat run after the binary already exists, prefer a read-only artifact
mount so Docker/redoxer never sees the repository tree:

```sh
RAW="$PWD/evaluation/raw/c007-redox-docker-redoxer-small-heap-20260707a"

docker run --rm --platform linux/amd64 \
  -v "$RAW:/input:ro" \
  redoxos/redoxer:latest \
  sh -lc '
    set -eu
    rm -rf /tmp/redox-stage
    mkdir /tmp/redox-stage
    cp /input/small_heap-redox /tmp/redox-stage/small_heap-redox
    chmod +x /tmp/redox-stage/small_heap-redox
    sha256sum /tmp/redox-stage/small_heap-redox
    redoxer exec -o - -f /tmp/redox-stage /root/redox-stage/small_heap-redox
  '
```

This path copies only the ~3 MiB executable into Docker.  Redoxer still creates a
temporary Redox disk of roughly 3 GiB logical size while the command runs, so do
not run many such probes concurrently.

## Current local host status

On this macOS host, Redox target-side source checks pass, Docker can run the
official `redoxos/redoxer` image, and the July 7, 2026 provenance capture fixed
the earlier target hang in UniAlloc fixed-heap radix-tree growth. The successful
run is:

- `evaluation/raw/c007-redox-docker-redoxer-provenance-deadlock-fix-20260707a/`
- transcript: `redoxer-small-heap.provenance.log`
- sparse image: `redoxer-preserved-image.bin`
- boot config: `redoxer-boot-config.json`
- validation log:
  `evaluation/raw/c007-redox-docker-redoxer-provenance-deadlock-fix-validate-20260707a.log`

The transcript contains `platform=redox`, `fixed_heap_ready=true`,
`c_abi_probe_passed=true`, positive semantic/type stats, and a trailing
`{"passed":true,...}` record. The validator reported
`ready_for_platform_import=true` and `claim_grade_ready=true`.

The validated claim boundary is therefore:

- ✅ Rust target/source/staticlib/C-ABI wiring can be checked locally.
- ✅ Docker/redoxer can run the UniAlloc Redox binary in a real Redox/QEMU target
  runtime with fixed-heap and C ABI counters.
- ✅ The preserved sparse image/config plus transcript are now hash-bound and
  pass `validate-constrained-boot-log` as claim-grade-ready Redox evidence.
- ⚠️ The aggregate platform matrix can still fail if other platform imports
  point at missing temporary artifacts. In the current workspace, BlogOS is the
  remaining missing-real-artifact blocker; do not fabricate replacement
  image/config files.
