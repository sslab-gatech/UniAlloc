#!/usr/bin/env python3
"""Require exact Clone scopes to authenticate rustc's Clone lang item."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
FIXTURE = (
    ROOT
    / "tools/unialloc-rustc-pass/fixtures/mir_clone_candidate_classification.rs"
)
EXACT_DESTINATIONS = {
    "std::string::String",
    "std::vec::Vec<u8, std::alloc::Global>",
}


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def clone_rows(audit: dict[str, object]) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and "::clone::Clone::clone" in str(row.get("callee") or "")
    ]


def compile_with_pass(
    pass_binary: Path,
    *,
    source: Path,
    crate_name: str,
    output: Path,
    audit: Path,
    sysroot: str,
    env: dict[str, str],
    extra: list[str] | None = None,
) -> dict[str, object]:
    run(
        [
            str(pass_binary),
            "--unialloc-rewrite-audit-out",
            str(audit),
            "--unialloc-continue-compilation",
            "--",
            "--sysroot",
            sysroot,
            "--crate-name",
            crate_name,
            "--edition=2021",
            "-Zmir-opt-level=0",
            str(source),
            *(extra or []),
            "-o",
            str(output),
        ],
        cwd=ROOT,
        env=env,
    )
    return json.loads(audit.read_text(encoding="utf-8"))


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    for variable in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        current = env.get(variable)
        env[variable] = f"{sysroot}/lib" + (
            os.pathsep + current if current else ""
        )

    with tempfile.TemporaryDirectory(prefix="unialloc-clone-lang-item-") as raw:
        workspace = Path(raw)
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        run(
            [
                rustc,
                f"+{toolchain}",
                "--cfg",
                "unialloc_rustc_current",
                str(PASS_SOURCE),
                "-O",
                "-o",
                str(pass_binary),
            ],
            cwd=ROOT,
            env=env,
        )

        fake_core = workspace / "fake_core.rs"
        fake_core.write_text(
            r'''#![crate_name = "core"]
pub mod clone {
    pub trait Clone: Sized {
        fn clone(value: &Self) -> Self;
    }

    impl Clone for std::vec::Vec<u8> {
        fn clone(value: &Self) -> Self {
            eprintln!("arbitrary user Clone code");
            value.as_slice().to_vec()
        }
    }
}

pub mod convert {
    pub trait From<T>: Sized {
        fn from(value: T) -> Self;
    }

    impl From<std::string::String> for std::vec::Vec<u8> {
        fn from(value: std::string::String) -> Self {
            let _unrelated = std::vec![
                <std::string::String as std::convert::From<&str>>::from("arbitrary")
            ];
            value.into_bytes()
        }
    }

    impl From<&'static str> for std::string::String {
        fn from(value: &'static str) -> Self {
            let _unrelated = std::vec![
                <std::string::String as std::convert::From<&str>>::from("arbitrary")
            ];
            <str as std::borrow::ToOwned>::to_owned(value)
        }
    }
}

pub mod slice {
    pub mod iter {
        pub struct Iter<T>(pub std::marker::PhantomData<T>);
    }
}

pub mod iter {
    pub mod adapters {
        pub mod copied {
            pub struct Copied<I>(pub I);
        }
    }

    pub mod traits {
        pub mod iterator {
            pub trait Iterator: Sized {
                fn collect<B>(self) -> B;
            }

            impl Iterator
                for crate::iter::adapters::copied::Copied<crate::slice::iter::Iter<u8>>
            {
                fn collect<B>(self) -> B {
                    let _unrelated = std::vec![
                        <std::string::String as std::convert::From<&str>>::from("nested")
                    ];
                    loop {}
                }
            }
        }
    }
}

pub mod mem {
    pub mod maybe_uninit {
        pub struct MaybeUninit<T>(pub T);

        impl<T> Drop for MaybeUninit<T> {
            fn drop(&mut self) {
                let _unrelated = std::vec![
                    <std::string::String as std::convert::From<&str>>::from("nested")
                ];
            }
        }
    }

    pub fn drop<T>(value: T) {
        let _unrelated = std::vec![
            <std::string::String as std::convert::From<&str>>::from("nested")
        ];
        std::mem::forget(value);
    }
}
''',
            encoding="utf-8",
        )
        fake_rlib = workspace / "libfake_core.rlib"
        run(
            [
                rustc,
                f"+{toolchain}",
                "--crate-type=rlib",
                "--edition=2021",
                str(fake_core),
                "-o",
                str(fake_rlib),
            ],
            cwd=ROOT,
            env=env,
        )

        spoof_app = workspace / "spoof_app.rs"
        spoof_app.write_text(
            r'''extern crate core;

trait EvilReserve {
    fn reserve(&mut self);
}

impl EvilReserve for Vec<u8> {
    fn reserve(&mut self) {
        let _unrelated = vec![std::string::String::from("callback")];
        self.push(9);
    }
}

fn spoof_clone() {
    let value = vec![1_u8, 2, 3];
    let _ = <Vec<u8> as core::clone::Clone>::clone(&value);
}

fn spoof_vec_from() {
    let source = std::string::String::new();
    let _ = <Vec<u8> as core::convert::From<std::string::String>>::from(source);
}

fn spoof_string_from() {
    let _ = <std::string::String as core::convert::From<&'static str>>::from("abc");
}

fn spoof_collect() {
    let iterator = core::iter::adapters::copied::Copied(
        core::slice::iter::Iter::<u8>(std::marker::PhantomData),
    );
    let _: Vec<u8> = <core::iter::adapters::copied::Copied<
        core::slice::iter::Iter<u8>,
    > as core::iter::traits::iterator::Iterator>::collect(iterator);
}

fn spoof_drop() {
    core::mem::drop(Box::new(7_u8));
}

fn spoof_drop_inert_wrapper() {
    let value = core::mem::maybe_uninit::MaybeUninit(7_u8);
    let _boxed = Box::new(value);
}

fn spoof_receiver_reserve() {
    let mut value = vec![1_u8, 2, 3];
    <Vec<u8> as EvilReserve>::reserve(&mut value);
}

fn main() {
    spoof_clone();
    spoof_vec_from();
    spoof_string_from();
    spoof_collect();
    spoof_drop();
    spoof_drop_inert_wrapper();
    spoof_receiver_reserve();
}
''',
            encoding="utf-8",
        )
        spoof_audit = compile_with_pass(
            pass_binary,
            source=spoof_app,
            crate_name="fake_core_spoof_app",
            output=workspace / "spoof-app",
            audit=workspace / "spoof-audit.json",
            sysroot=sysroot,
            env=env,
            extra=["--extern", f"core={fake_rlib}"],
        )
        spoof_rows = clone_rows(spoof_audit)
        assert len(spoof_rows) == 1, spoof_rows
        spoof = spoof_rows[0]
        assert spoof.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), spoof
        assert spoof.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), spoof
        assert spoof.get("replacement_resolution_status") == (
            "rustc_middle_heap_object_type_not_solved"
        ), spoof
        assert spoof.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), spoof
        assert spoof.get("semantic_object_type") == "<unknown-heap-object-type>", spoof
        spoof_from_rows = [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and "::convert::From::from" in str(row.get("callee") or "")
            and "Vec<u8" in str(row.get("destination_type") or "")
        ]
        assert len(spoof_from_rows) == 1, spoof_from_rows
        spoof_from = spoof_from_rows[0]
        assert spoof_from.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), spoof_from
        assert spoof_from.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
        ), spoof_from
        assert spoof_from.get("metadata_pairing_contract") == (
            "audit_only_ambiguous_heap_object_type"
        ), spoof_from
        spoof_string_from_rows = [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("spoof_string_from")
            and "::convert::From::from" in str(row.get("callee") or "")
        ]
        assert len(spoof_string_from_rows) == 1, spoof_string_from_rows
        spoof_string_from = spoof_string_from_rows[0]
        assert spoof_string_from.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), spoof_string_from
        assert spoof_string_from.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), spoof_string_from
        assert spoof_string_from.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), spoof_string_from
        spoof_collect_rows = [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("spoof_collect")
            and "::Iterator::collect" in str(row.get("callee") or "")
        ]
        assert not [
            row
            for row in spoof_collect_rows
            if "rewrite_planned" in str(row.get("rewrite_status") or "")
            or "rewrite_applied" in str(row.get("rewrite_status") or "")
        ], spoof_collect_rows
        spoof_drop_rows = [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("spoof_drop")
            and "::mem::drop" in str(row.get("callee") or "")
        ]
        assert not [
            row
            for row in spoof_drop_rows
            if "rewrite_planned" in str(row.get("rewrite_status") or "")
            or "rewrite_applied" in str(row.get("rewrite_status") or "")
        ], spoof_drop_rows
        spoof_drop_inert_rows = [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(
                "spoof_drop_inert_wrapper"
            )
            and row.get("lowering_kind")
            == "semantic_scope_drop_exact_box_recovery_skipped"
        ]
        assert len(spoof_drop_inert_rows) == 1, [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and "spoof_drop_inert_wrapper" in str(row.get("mir_function") or "")
        ]
        spoof_receiver_rows = [
            row
            for row in spoof_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(
                "spoof_receiver_reserve"
            )
            and "EvilReserve::reserve" in str(row.get("callee") or "")
        ]
        assert spoof_receiver_rows, spoof_receiver_rows
        assert not [
            row
            for row in spoof_receiver_rows
            if "rewrite_planned" in str(row.get("rewrite_status") or "")
            or "rewrite_applied" in str(row.get("rewrite_status") or "")
        ], spoof_receiver_rows

        fake_alloc = workspace / "fake_alloc.rs"
        fake_alloc.write_text(
            r'''#![crate_name = "alloc"]
pub mod alloc {
    pub struct Global;

    pub unsafe fn alloc(layout: std::alloc::Layout) -> *mut u8 {
        let _unrelated = std::vec![std::string::String::from("arbitrary")];
        std::alloc::alloc(layout)
    }
}

pub mod vec {
    pub struct Vec<T, A>(pub std::vec::Vec<T>, pub std::marker::PhantomData<A>);

    impl core::clone::Clone for Vec<u8, crate::alloc::Global> {
        fn clone(&self) -> Self {
            let _unrelated = std::string::String::from("arbitrary user allocation");
            Self(self.0.clone(), std::marker::PhantomData)
        }
    }
}

pub mod string {
    pub struct String(pub std::string::String);

    impl String {
        pub fn into_bytes(self) -> crate::vec::Vec<u8, crate::alloc::Global> {
            let _unrelated = std::vec![std::string::String::from("arbitrary")];
            crate::vec::Vec(std::vec![9_u8], std::marker::PhantomData)
        }
    }
}

pub mod borrow {
    pub trait ToOwned {
        type Owned;
        fn to_owned(&self) -> Self::Owned;
    }

    impl ToOwned for str {
        type Owned = crate::string::String;

        fn to_owned(&self) -> Self::Owned {
            let _unrelated = std::vec![std::string::String::from("arbitrary")];
            crate::string::String(std::string::String::from(self))
        }
    }
}
''',
            encoding="utf-8",
        )
        fake_alloc_rlib = workspace / "libfake_alloc.rlib"
        run(
            [
                rustc,
                f"+{toolchain}",
                "--crate-type=rlib",
                "--edition=2021",
                str(fake_alloc),
                "-o",
                str(fake_alloc_rlib),
            ],
            cwd=ROOT,
            env=env,
        )
        fake_owner_app = workspace / "fake_owner_app.rs"
        fake_owner_app.write_text(
            r'''extern crate alloc;

fn spoof_clone_owner() {
    let value = alloc::vec::Vec::<u8, alloc::alloc::Global>(
        std::vec![1_u8],
        std::marker::PhantomData,
    );
    let _ = core::clone::Clone::clone(&value);
}

fn spoof_to_owned() {
    let _ = <str as alloc::borrow::ToOwned>::to_owned("abc");
}

fn spoof_into_bytes() {
    let value = alloc::string::String(std::string::String::new());
    let _ = value.into_bytes();
}

unsafe fn spoof_direct_alloc() {
    let layout = std::alloc::Layout::new::<u64>();
    let pointer = alloc::alloc::alloc(layout);
    if !pointer.is_null() {
        std::alloc::dealloc(pointer, layout);
    }
}

fn main() {
    spoof_clone_owner();
    spoof_to_owned();
    spoof_into_bytes();
    unsafe { spoof_direct_alloc(); }
}
''',
            encoding="utf-8",
        )
        fake_owner_audit = compile_with_pass(
            pass_binary,
            source=fake_owner_app,
            crate_name="fake_alloc_spoof_app",
            output=workspace / "fake-owner-app",
            audit=workspace / "fake-owner-audit.json",
            sysroot=sysroot,
            env=env,
            extra=["--extern", f"alloc={fake_alloc_rlib}"],
        )
        fake_owner_rows = clone_rows(fake_owner_audit)
        assert len(fake_owner_rows) == 1, fake_owner_rows
        fake_owner = fake_owner_rows[0]
        assert fake_owner.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), fake_owner
        assert fake_owner.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), fake_owner
        assert fake_owner.get("replacement_resolution_status") == (
            "rustc_middle_heap_object_type_not_solved"
        ), fake_owner
        assert fake_owner.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), fake_owner
        assert (
            fake_owner.get("semantic_object_type") == "<unknown-heap-object-type>"
        ), fake_owner
        fake_to_owned_rows = [
            row
            for row in fake_owner_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("spoof_to_owned")
            and "::borrow::ToOwned::to_owned" in str(row.get("callee") or "")
        ]
        assert len(fake_to_owned_rows) == 1, fake_to_owned_rows
        fake_to_owned = fake_to_owned_rows[0]
        assert fake_to_owned.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), fake_to_owned
        assert fake_to_owned.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), fake_to_owned
        assert fake_to_owned.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), fake_to_owned
        fake_into_bytes_rows = [
            row
            for row in fake_owner_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("spoof_into_bytes")
            and "::into_bytes" in str(row.get("callee") or "")
        ]
        assert not [
            row
            for row in fake_into_bytes_rows
            if "rewrite_planned" in str(row.get("rewrite_status") or "")
            or "rewrite_applied" in str(row.get("rewrite_status") or "")
        ], fake_into_bytes_rows
        fake_direct_alloc_rows = [
            row
            for row in fake_owner_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("spoof_direct_alloc")
            and row.get("lowering_kind") == "direct_allocator_call_rewrite"
            and "::alloc::alloc)" in str(row.get("callee") or "")
        ]
        assert not fake_direct_alloc_rows, fake_direct_alloc_rows

        legitimate_audit = compile_with_pass(
            pass_binary,
            source=FIXTURE,
            crate_name="mir_clone_candidate_classification",
            output=workspace / "legitimate-fixture",
            audit=workspace / "legitimate-audit.json",
            sysroot=sysroot,
            env=env,
        )
        legitimate_exact = [
            row
            for row in clone_rows(legitimate_audit)
            if row.get("destination_type") in EXACT_DESTINATIONS
            and row.get("rewrite_status")
            == "semantic_scope_enter_exit_rewrite_planned"
        ]
        assert len(legitimate_exact) == 7, legitimate_exact
        assert all(
            row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and row.get("metadata_pairing_contract")
            == "semantic_scope_active_metadata"
            for row in legitimate_exact
        ), legitimate_exact

        legitimate_from = workspace / "legitimate_from.rs"
        legitimate_from.write_text(
            r'''fn main() {
    let source = std::string::String::from("abc");
    let bytes = std::vec::Vec::<u8>::from(source);
    assert_eq!(bytes, b"abc");
}
''',
            encoding="utf-8",
        )
        legitimate_from_audit = compile_with_pass(
            pass_binary,
            source=legitimate_from,
            crate_name="legitimate_core_from",
            output=workspace / "legitimate-from",
            audit=workspace / "legitimate-from-audit.json",
            sysroot=sysroot,
            env=env,
        )
        legitimate_from_rows = [
            row
            for row in legitimate_from_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and row.get("rewrite_status")
            == "semantic_ownership_transfer_rewrite_planned"
            and row.get("lowering_kind") == "semantic_ownership_transfer_rewrite"
            and row.get("metadata_pairing_contract")
            == "pointer_preserving_owner_identity_rebind"
            and "::convert::From::from" in str(row.get("callee") or "")
        ]
        assert len(legitimate_from_rows) == 1, legitimate_from_rows

        legitimate_alloc = workspace / "legitimate_alloc.rs"
        legitimate_alloc.write_text(
            r'''struct Dropper;

impl Drop for Dropper {
    fn drop(&mut self) {
        let _unrelated = vec![std::string::String::from("drop callback")];
    }
}

unsafe fn real_alloc() {
    let layout = std::alloc::Layout::new::<u64>();
    let pointer = std::alloc::alloc(layout);
    if !pointer.is_null() {
        std::alloc::dealloc(pointer, layout);
    }
}

fn dropful_vec() {
    let mut values = std::vec::Vec::new();
    values.push(Dropper);
}

fn main() {
    unsafe { real_alloc(); }
    dropful_vec();
}
''',
            encoding="utf-8",
        )
        legitimate_alloc_audit = compile_with_pass(
            pass_binary,
            source=legitimate_alloc,
            crate_name="legitimate_allocator_authentication",
            output=workspace / "legitimate-alloc",
            audit=workspace / "legitimate-alloc-audit.json",
            sysroot=sysroot,
            env=env,
        )
        legitimate_direct_alloc_rows = [
            row
            for row in legitimate_alloc_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("real_alloc")
            and row.get("lowering_kind") == "direct_allocator_call_rewrite"
        ]
        assert len(legitimate_direct_alloc_rows) == 2, legitimate_direct_alloc_rows
        dropful_vec_rows = [
            row
            for row in legitimate_alloc_audit.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("dropful_vec")
            and row.get("lowering_kind")
            == "semantic_scope_drop_effectful_owner_recovery_skipped"
        ]
        assert len(dropful_vec_rows) == 2, dropful_vec_rows

    print(
        json.dumps(
            {
                "source": "mir_clone_lang_item_authentication",
                "validated": True,
                "spoof_clone_rows": 1,
                "spoof_applied_or_planned_scopes": 0,
                "spoof_from_rows": 1,
                "spoof_from_applied_or_planned_scopes": 0,
                "spoof_string_from_rows": 1,
                "spoof_string_from_applied_or_planned_scopes": 0,
                "spoof_collect_applied_or_planned_scopes": 0,
                "spoof_drop_applied_or_planned_scopes": 0,
                "spoof_drop_inert_wrapper_recovery_rows": 1,
                "spoof_receiver_applied_or_planned_scopes": 0,
                "fake_owner_clone_rows": 1,
                "fake_owner_applied_or_planned_scopes": 0,
                "fake_to_owned_rows": 1,
                "fake_to_owned_applied_or_planned_scopes": 0,
                "fake_into_bytes_applied_or_planned_rewrites": 0,
                "fake_direct_alloc_rewrites": 0,
                "legitimate_exact_clone_rows": 7,
                "legitimate_exact_from_rows": 1,
                "legitimate_direct_allocator_rows": 2,
                "legitimate_dropful_owner_recovery_rows": 2,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
