#!/usr/bin/env python3
"""Regressions for pinned RustSec heap-harness materialization and containment."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER_PATH = REPOSITORY_ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"

spec = importlib.util.spec_from_file_location("unialloc_rustsec_heap_runner", RUNNER_PATH)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_bytes(root: pathlib.Path, relative: str, value: bytes) -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return {"path": relative, "sha256": sha256_bytes(value)}


def write_crate(
    path: pathlib.Path,
    *,
    root_name: str = "demo_crate-1.2.3",
    files: dict[str, bytes] | None = None,
) -> None:
    files = files or {
        "Cargo.toml": b'[package]\nname = "demo_crate"\nversion = "1.2.3"\n',
        "src/lib.rs": b"pub const VALUE: i32 = 1;\n",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo(root_name)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for relative, contents in sorted(files.items()):
            member = tarfile.TarInfo(f"{root_name}/{relative}")
            member.size = len(contents)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(contents))


class RustSecHeapHarnessRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.original_root = runner.ROOT
        runner.ROOT = self.root
        self.addCleanup(setattr, runner, "ROOT", self.original_root)

        source = b"fn main() { println!(\"pinned harness\"); }\n"
        source_meta = write_bytes(
            self.root,
            "evaluation/harnesses/rustsec_heap/RSH-999/main.rs",
            source,
        )
        vulnerable_lock = b"# vulnerable lock\nversion = 3\n"
        patched_lock = b"# patched lock\nversion = 3\n"
        vulnerable_lock_meta = write_bytes(
            self.root,
            "evaluation/harnesses/rustsec_heap/RSH-999/Cargo.lock.vulnerable",
            vulnerable_lock,
        )
        patched_lock_meta = write_bytes(
            self.root,
            "evaluation/harnesses/rustsec_heap/RSH-999/Cargo.lock.patched",
            patched_lock,
        )

        self.cache = self.root / "cache"
        self.archive = self.cache / "archives" / "demo_crate-1.2.3.crate"
        write_crate(self.archive)
        archive_bytes = self.archive.read_bytes()
        dependency = {
            "kind": "crates_io_archive",
            "package": "demo_crate",
            "version": "1.2.3",
            "url": "https://static.crates.io/crates/demo_crate/demo_crate-1.2.3.crate",
            "bytes": len(archive_bytes),
            "sha256": sha256_bytes(archive_bytes),
            "upstream_commit": "a" * 40,
        }
        self.catalog = {
            "schema_version": 1,
            "source": "synthetic-test-catalog",
            "claim_grade": False,
            "counts": {
                "harness_case_count": 1,
                "repository_scenario_count": 1,
            },
            "cases": [
                {
                    "case_id": "RSH-999",
                    "crate": "demo_crate",
                    "cargo": {
                        "default_features": False,
                        "features": ["synthetic"],
                        "vulnerable_extra_dependencies": {"cfg-if": "=1.0.0"},
                        "patched_extra_dependencies": {"cfg-if": "=1.0.0"},
                    },
                    "vulnerable_dependency": dependency,
                    "patched_dependency": copy.deepcopy(dependency),
                    "lockfiles": {
                        "vulnerable": vulnerable_lock_meta,
                        "patched": patched_lock_meta,
                    },
                    "scenarios": [
                        {
                            "scenario_id": "RSH-999-synthetic",
                            "source_path": source_meta["path"],
                            "source_sha256": source_meta["sha256"],
                            "classification_role": "published_negative_control",
                            "cargo_profile": "dev",
                            "rustflags": ["-Copt-level=1"],
                            "required_environment": {},
                            "oracle": {
                                "tool": "native",
                                "vulnerable": "synthetic finding",
                                "patched_control": "clean exit",
                            },
                        }
                    ],
                }
            ],
        }

    def write_catalog(self) -> pathlib.Path:
        path = self.root / "catalog.json"
        path.write_text(json.dumps(self.catalog), encoding="utf-8")
        return path

    def materialize(self, *, variant: str = "vulnerable", work_name: str = "work"):
        return runner.materialize(
            self.catalog,
            "RSH-999-synthetic",
            variant,
            self.root / work_name,
            self.cache,
            False,
        )

    def test_list_action_outputs_catalog_counts_and_scenario(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = runner.main(["--catalog", str(self.write_catalog()), "--action", "list"])

        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["counts"], self.catalog["counts"])
        self.assertEqual(
            payload["scenarios"],
            [
                {
                    "case_id": "RSH-999",
                    "classification_role": "published_negative_control",
                    "crate": "demo_crate",
                    "scenario_id": "RSH-999-synthetic",
                    "tool": "native",
                }
            ],
        )

    def test_materialize_requires_explicit_work_directory_before_work_begins(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(runner, "materialize") as materialize_mock:
            with contextlib.redirect_stderr(stderr):
                status = runner.main(
                    [
                        "--catalog",
                        str(self.write_catalog()),
                        "--action",
                        "materialize",
                        "--scenario",
                        "RSH-999-synthetic",
                    ]
                )

        self.assertEqual(status, 2)
        materialize_mock.assert_not_called()
        self.assertIn("--work-dir is required", stderr.getvalue())

    def test_main_threads_build_timeout_to_contained_check(self) -> None:
        work = self.root / "main-check-work"
        with mock.patch.object(runner.shutil, "which", return_value="/usr/bin/docker"), mock.patch.object(
            runner, "container_check", return_value=0
        ) as contained_check:
            status = runner.main(
                [
                    "--catalog",
                    str(self.write_catalog()),
                    "--action",
                    "check",
                    "--scenario",
                    "RSH-999-synthetic",
                    "--work-dir",
                    str(work),
                    "--cache",
                    str(self.cache),
                    "--build-timeout",
                    "123",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(contained_check.call_args.kwargs["timeout"], 123)

    def test_materialization_pins_source_archive_manifest_and_lock(self) -> None:
        report = self.materialize()
        work = self.root / "work"

        self.assertEqual(
            (work / "project/src/main.rs").read_bytes(),
            b"fn main() { println!(\"pinned harness\"); }\n",
        )
        self.assertEqual(
            (work / "project/Cargo.lock").read_bytes(),
            b"# vulnerable lock\nversion = 3\n",
        )
        manifest = tomllib.loads((work / "project/Cargo.toml").read_text(encoding="utf-8"))
        dependency = manifest["dependencies"]["demo_crate"]
        self.assertEqual(dependency["path"], "../subject/demo_crate-1.2.3")
        self.assertFalse(dependency["default-features"])
        self.assertEqual(dependency["features"], ["synthetic"])
        self.assertEqual(manifest["dependencies"]["cfg-if"], "=1.0.0")
        self.assertEqual(report["archive_sha256"], sha256_bytes(self.archive.read_bytes()))
        self.assertEqual(
            report["source_sha256"],
            sha256_bytes((work / "project/src/main.rs").read_bytes()),
        )
        self.assertEqual(
            report["cargo_lock_sha256"],
            sha256_bytes((work / "project/Cargo.lock").read_bytes()),
        )
        config = (work / "project/.cargo/config.toml").read_text(encoding="utf-8")
        self.assertIn("-Ainvalid_reference_casting", config)
        self.assertIn("-Adangerous_implicit_autorefs", config)
        self.assertIn("-Copt-level=1", config)

    def test_materialization_rejects_source_hash_mismatch(self) -> None:
        self.catalog["cases"][0]["scenarios"][0]["source_sha256"] = "0" * 64

        with self.assertRaisesRegex(runner.HarnessError, "repository file hash mismatch"):
            self.materialize()

    def test_materialization_rejects_archive_hash_mismatch(self) -> None:
        self.catalog["cases"][0]["vulnerable_dependency"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(runner.HarnessError, "archive SHA-256 mismatch"):
            self.materialize()

    def test_safe_extract_rejects_path_traversal_and_cleans_destination(self) -> None:
        malicious = self.root / "malicious.crate"
        with tarfile.open(malicious, "w:gz") as archive:
            member = tarfile.TarInfo("crate-1.0.0/../../escaped")
            contents = b"escape"
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
        destination = self.root / "extract"

        with self.assertRaisesRegex(runner.HarnessError, "unsafe archive member"):
            runner.safe_extract_crate(malicious, destination)

        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "escaped").exists())

    @unittest.skipUnless(shutil.which("patch"), "patch executable is required")
    def test_local_patched_control_uses_vulnerable_archive_and_verifies_result(self) -> None:
        patch = (
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1 +1 @@\n"
            "-pub const VALUE: i32 = 1;\n"
            "+pub const VALUE: i32 = 2;\n"
        ).encode()
        patch_meta = write_bytes(
            self.root,
            "evaluation/harnesses/rustsec_heap/RSH-999/patches/control.patch",
            patch,
        )
        patched_value = b"pub const VALUE: i32 = 2;\n"
        scenario = self.catalog["cases"][0]["scenarios"][0]
        scenario["local_patched_control"] = {
            "kind": "hashed_local_patch",
            "label": "synthetic fix",
            **patch_meta,
            "resulting_source_files": {
                "src/lib.rs": sha256_bytes(patched_value),
            },
        }
        self.catalog["cases"][0]["patched_dependency"] = {
            "kind": "scenario_local_patches"
        }

        report = self.materialize(variant="patched")

        subject = pathlib.Path(report["subject_directory"])
        self.assertEqual((subject / "src/lib.rs").read_bytes(), patched_value)
        self.assertEqual(report["archive_sha256"], sha256_bytes(self.archive.read_bytes()))
        self.assertEqual(
            report["local_patched_control"]["resulting_source_files"],
            {"src/lib.rs": sha256_bytes(patched_value)},
        )
        self.assertEqual(
            (self.root / "work/project/Cargo.lock").read_bytes(),
            b"# patched lock\nversion = 3\n",
        )

    @unittest.skipUnless(shutil.which("patch"), "patch executable is required")
    def test_local_patch_rejects_unpinned_result(self) -> None:
        patch = (
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1 +1 @@\n"
            "-pub const VALUE: i32 = 1;\n"
            "+pub const VALUE: i32 = 2;\n"
        ).encode()
        patch_meta = write_bytes(
            self.root,
            "evaluation/harnesses/rustsec_heap/RSH-999/patches/control.patch",
            patch,
        )
        scenario = self.catalog["cases"][0]["scenarios"][0]
        scenario["local_patched_control"] = {
            "kind": "hashed_local_patch",
            "label": "synthetic fix",
            **patch_meta,
            "resulting_source_files": {"src/lib.rs": "0" * 64},
        }
        self.catalog["cases"][0]["patched_dependency"] = {
            "kind": "scenario_local_patches"
        }

        with self.assertRaisesRegex(runner.HarnessError, "patched subject hash mismatch"):
            self.materialize(variant="patched")

    @unittest.skipUnless(shutil.which("patch"), "patch executable is required")
    def test_subject_patch_applies_to_both_archived_variants_and_pins_each_result(self) -> None:
        patch = (
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1 +1 @@\n"
            "-pub const VALUE: i32 = 1;\n"
            "+pub const VALUE: i32 = 1; // adapter visibility\n"
        ).encode()
        patch_meta = write_bytes(
            self.root,
            "evaluation/harnesses/rustsec_heap/RSH-999/patches/adapter.patch",
            patch,
        )
        adapted_value = b"pub const VALUE: i32 = 1; // adapter visibility\n"
        scenario = self.catalog["cases"][0]["scenarios"][0]
        scenario["subject_patches"] = [
            {
                "kind": "hashed_subject_patch",
                "label": "test-only API exposure",
                **patch_meta,
                "apply_variants": ["vulnerable", "patched"],
                "resulting_source_files_by_variant": {
                    "vulnerable": {"src/lib.rs": sha256_bytes(adapted_value)},
                    "patched": {"src/lib.rs": sha256_bytes(adapted_value)},
                },
            }
        ]

        vulnerable = self.materialize(variant="vulnerable", work_name="vulnerable-work")
        patched = self.materialize(variant="patched", work_name="patched-work")

        for report in (vulnerable, patched):
            subject = pathlib.Path(report["subject_directory"])
            self.assertEqual((subject / "src/lib.rs").read_bytes(), adapted_value)
            self.assertEqual(
                report["subject_patches"][0]["resulting_source_files"],
                {"src/lib.rs": sha256_bytes(adapted_value)},
            )

    def test_subject_patch_rejects_missing_variant_result_hashes(self) -> None:
        scenario = self.catalog["cases"][0]["scenarios"][0]
        scenario["subject_patches"] = [
            {
                "kind": "hashed_subject_patch",
                "label": "incomplete adapter",
                "path": scenario["source_path"],
                "sha256": scenario["source_sha256"],
                "apply_variants": ["vulnerable", "patched"],
                "resulting_source_files_by_variant": {
                    "vulnerable": {"src/lib.rs": "0" * 64}
                },
            }
        ]

        with self.assertRaisesRegex(runner.HarnessError, "missing pinned patched results"):
            self.materialize(variant="patched")

    def test_container_check_fetches_then_compiles_offline_with_limits(self) -> None:
        work = self.root / "container-work"
        (work / "subject").mkdir(parents=True)
        calls: list[list[str]] = []

        timeouts: list[float | None] = []

        def record(command, *, capture=False, timeout_seconds=None):
            del capture
            calls.append(command)
            timeouts.append(timeout_seconds)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(runner, "run_checked", side_effect=record):
            status = runner.container_check(
                "pinned-image@sha256:digest",
                self.cache,
                work,
                "release",
                {"RUSTC_BOOTSTRAP": "1", "platform": "descriptive-only"},
                timeout=73,
            )

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(timeouts, [73, 73])
        self.assertIn("fetch", calls[0])
        self.assertNotIn("none", calls[0][0 : calls[0].index("pinned-image@sha256:digest")])
        self.assertIn("--network", calls[1])
        self.assertIn("none", calls[1])
        self.assertIn("--offline", calls[1])
        self.assertIn("--locked", calls[1])
        self.assertIn("--release", calls[1])
        self.assertIn("--read-only", calls[1])
        self.assertIn("--cap-drop=ALL", calls[1])
        self.assertIn("--pids-limit=256", calls[1])
        self.assertIn("RUSTC_BOOTSTRAP=1", calls[1])
        self.assertNotIn("platform=descriptive-only", calls[1])
        fetch_mounts = [
            calls[0][index + 1]
            for index, value in enumerate(calls[0][:-1])
            if value == "--mount"
        ]
        check_mounts = [
            calls[1][index + 1]
            for index, value in enumerate(calls[1][:-1])
            if value == "--mount"
        ]
        self.assertTrue(
            any("dst=/cargo-cache" in value and "readonly" not in value for value in fetch_mounts)
        )
        self.assertFalse(any("dst=/cargo-cache-ro" in value for value in fetch_mounts))
        self.assertTrue(
            any("dst=/cargo-cache-ro" in value and "readonly" in value for value in check_mounts)
        )
        self.assertFalse(any("dst=/cargo-cache," in value for value in check_mounts))
        self.assertIn("CARGO_HOME=/cargo-home", calls[1])
        self.assertTrue(
            any(value.startswith("/cargo-home:") for value in calls[1])
        )
        self.assertIn(runner.CARGO_HOME_OVERLAY_SCRIPT, calls[1])
        for command, phase in zip(calls, ("fetch", "check"), strict=True):
            name = command[command.index("--name") + 1]
            self.assertRegex(
                name, rf"^unialloc-rustsec-{phase}-[0-9a-f]{{32}}$"
            )
        workdir_index = calls[1].index("--workdir")
        self.assertEqual(calls[1][workdir_index + 1], "/work/project")

    def test_container_build_uses_rw_fetch_and_ro_offline_cache(self) -> None:
        work = self.root / "container-build-work"
        (work / "subject").mkdir(parents=True)
        calls: list[list[str]] = []

        def record(command, *, capture=False, timeout_seconds=None):
            del capture, timeout_seconds
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(runner, "run_checked", side_effect=record):
            status = runner.container_build(
                "pinned-image@sha256:digest",
                self.cache,
                work,
                "dev",
                timeout=81,
            )

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("fetch", calls[0])
        self.assertIn("build", calls[1])
        self.assertIn("--offline", calls[1])
        build_mounts = [
            calls[1][index + 1]
            for index, value in enumerate(calls[1][:-1])
            if value == "--mount"
        ]
        self.assertTrue(
            any("dst=/cargo-cache-ro" in value and "readonly" in value for value in build_mounts)
        )
        self.assertFalse(any("dst=/cargo-cache," in value for value in build_mounts))

    def test_fetch_check_and_build_timeouts_force_named_cleanup(self) -> None:
        operations = (
            ("fetch", runner.container_check, [subprocess.TimeoutExpired([], 7)]),
            (
                "check",
                runner.container_check,
                [
                    subprocess.CompletedProcess([], 0),
                    subprocess.TimeoutExpired([], 7),
                ],
            ),
            (
                "build",
                runner.container_build,
                [
                    subprocess.CompletedProcess([], 0),
                    subprocess.TimeoutExpired([], 7),
                ],
            ),
        )
        for phase, operation, effects in operations:
            with self.subTest(phase=phase):
                work = self.root / f"timeout-{phase}"
                (work / "subject").mkdir(parents=True)
                with mock.patch.object(
                    runner, "run_checked", side_effect=effects
                ) as checked, mock.patch.object(
                    runner.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as cleanup:
                    with self.assertRaisesRegex(runner.HarnessError, "hard timeout"):
                        operation(
                            "pinned-image@sha256:digest",
                            self.cache,
                            work,
                            "dev",
                            timeout=7,
                        )

                timed_call = checked.call_args_list[-1]
                self.assertEqual(timed_call.kwargs["timeout_seconds"], 7)
                timed_command = timed_call.args[0]
                container_name = timed_command[timed_command.index("--name") + 1]
                self.assertRegex(
                    container_name,
                    rf"^unialloc-rustsec-{phase}-[0-9a-f]{{32}}$",
                )
                self.assertEqual(
                    cleanup.call_args.args[0],
                    ["docker", "rm", "-f", container_name],
                )

    def test_native_execution_command_is_read_only_unprivileged_and_offline(self) -> None:
        work = self.root / "native-work"
        binary = work / "project/target/debug/rsh-999-harness"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"synthetic executable")

        with mock.patch.object(
            runner,
            "run_checked",
            return_value=subprocess.CompletedProcess([], 0),
        ) as checked:
            status = runner.container_run_native(
                "pinned-image@sha256:digest",
                work,
                "RSH-999",
                "dev",
                5,
                {"ASAN_OPTIONS": "detect_leaks=0", "platform": "ignored"},
            )

        self.assertEqual(status, 0)
        command = checked.call_args.args[0]
        self.assertIn("--network", command)
        self.assertIn("none", command)
        self.assertIn("--read-only", command)
        self.assertIn("65534:65534", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--pids-limit=64", command)
        self.assertIn("ASAN_OPTIONS=detect_leaks=0", command)
        self.assertNotIn("platform=ignored", command)
        self.assertIn("timeout", command)
        self.assertIn("--signal=TERM", command)
        self.assertIn("--kill-after=2s", command)
        self.assertIn("5", command)

    def test_native_host_timeout_force_removes_the_container(self) -> None:
        work = self.root / "native-timeout-work"
        binary = work / "project/target/debug/rsh-999-harness"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"synthetic executable")

        expired = subprocess.TimeoutExpired(["docker", "run"], timeout=15)
        with mock.patch.object(runner, "run_checked", side_effect=expired), mock.patch.object(
            runner.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as cleanup:
            with self.assertRaisesRegex(runner.HarnessError, "hard timeout"):
                runner.container_run_native(
                    "pinned-image@sha256:digest", work, "RSH-999", "dev", 5
                )

        cleanup_command = cleanup.call_args.args[0]
        self.assertEqual(cleanup_command[:3], ["docker", "rm", "-f"])
        self.assertRegex(
            cleanup_command[3], r"^unialloc-rustsec-run-[0-9a-f]{32}$"
        )

    def test_container_environment_rejects_nul_bytes(self) -> None:
        with self.assertRaisesRegex(runner.HarnessError, "invalid environment value"):
            runner.container_environment_args(
                {"RUSTC_BOOTSTRAP": "1\0injected"}, phase="build"
            )


if __name__ == "__main__":
    unittest.main()
