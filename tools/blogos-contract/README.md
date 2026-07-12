# BlogOS fixed-heap build contract

This standalone no_std target is the smallest local BlogOS integration fixture
for UniAlloc.  It connects four kernel-side responsibilities in one linked ELF:

1. the boot path publishes an already mapped, writable heap range exactly once;
   later calls only succeed when they name that exact published range;
2. a guarded `#[global_allocator]` rejects allocation before initialization and
   delegates to UniAlloc afterward;
3. the boot contract performs one real `Box` allocation/deallocation; and
4. panic and allocation-error handlers end in a non-allocating halt loop.

Run the regression with the repository-pinned nightly:

```sh
python3 tools/blogos-contract/test_blogos_contract.py
```

The script builds `x86_64-unknown-none` with `core` and `alloc`, links the ELF,
and checks the `_start`, `blogos_unialloc_boot_init`, and
`blogos_unialloc_contract_probe` symbols.  No bootloader, image, or QEMU asset is
needed for this local contract check.

The BSS-backed heap in `src/main.rs` exists only so this repository-local target
can link the complete handoff without external assets.  A real BlogOS kernel
must instead pass the heap range mapped by its boot/memory-management code and
must keep that range exclusively owned by UniAlloc.  The boot wrapper must also
be the only fixed-heap initialization path; it fails closed if another path has
already made UniAlloc ready, while concurrent cross-API initialization remains
forbidden by its safety contract.  This contract is build, link, and ABI
evidence; it is not BlogOS runtime/boot or performance evidence.
