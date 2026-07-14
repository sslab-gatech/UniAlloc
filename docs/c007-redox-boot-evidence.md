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
    --no-default-features --features fixed_heap,allow_mem_leak,stats,type_isolation

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
The timeout is a wall-clock bound: a silent redoxer/QEMU hang is terminated even
when the child has not emitted another log line.
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

## Current-source compile and C ABI contract

The bounded functional check below does not require a running VM:

```sh
python3 evaluation/scripts/redox_current_source_contract.py \
  --toolchain "$(cat rust-toolchain)" \
  --target x86_64-unknown-redox \
  --output-dir /tmp/unialloc-redox-current-source
```

It checks the real allocator library and `small_heap` example for the Redox
target, emits an x86-64 Redox ELF relocatable object, and verifies the fixed-heap,
metadata, semantic-stats, and constrained-boot C ABI exports with the toolchain's
`llvm-nm`.  This closes the local source/codegen/interface contract only.  A
final Redox executable still needs the Redox linker supplied by redoxer, and a
boot/run statement still needs a real redoxer/QEMU transcript.  A host `cc`
linker failure is therefore an external toolchain integration gap, not an
allocator functional failure.

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

For the implementation-first G002 scope, commits `00a187b` and `bd9d927` close
two local source gaps without changing that external-evidence boundary.  The
BlogOS fixture now links a no_std fixed-heap/global-allocator/boot/panic contract,
and the Rust-for-Linux final crate explicitly force-links UniAlloc while a
checked-in no_std regression verifies the real bridge's allocator and semantic
symbols.  Commit `a581cb4` additionally makes the BlogOS heap-publication state
machine host-testable without changing its no_std target implementation.  Its
five host behavior regressions pass: first publication invokes initialization
once, same-range publication is idempotent, a different range is rejected,
failed initialization can be retried, and concurrent callers wait and resolve
against the winning range.  The same contract runner separately links the
`x86_64-unknown-none` fixture and verifies its allocator/boot/handler wiring; the
linked ELF SHA-256 is
`dcb668f3435a68bc29a16b315310cb0956b4d1603a33052aa761f049c00d994b`.
These host behavior tests and linked-image checks are behavior/wiring evidence,
not a BlogOS runtime boot validation.  Commit `315cfc4` additionally checks five
runtime/bridge ABI records'
versions, sizes, alignments, and all 75 field offsets at Rust compile time, with
matching C-header static assertions.  The Rust assertions compile for
`x86_64-unknown-none`; the C header check uses the local 64-bit host compiler,
not the actual kernel compiler.  The target-side results remain build/link/ABI
evidence only; the host behavior regressions do not upgrade either fixture to a
real target-runtime result:
BlogOS still needs its real
bootloader/image plus QEMU or hardware, and Rust-for-Linux still needs a kernel
tree and module runner.  Those missing assets are external validation gaps, not
allocator functional failures.
