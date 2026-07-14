#!/usr/bin/env python3
"""Regression tests for the RRedis redis-benchmark JSON bridge claim contract."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
WRAPPER_PATH = ROOT / "evaluation" / "scripts" / "paper_external_redis_benchmark_json.py"

spec = importlib.util.spec_from_file_location("paper_external_redis_benchmark_json", WRAPPER_PATH)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def args_for(allocator: str, source_contract: dict) -> argparse.Namespace:
    return argparse.Namespace(
        dataset="default_performance",
        benchmark="RRedis*",
        allocator=allocator,
        variant_feature="",
        run_index="1",
        claim_grade_contract="rredis-redis-benchmark-current-run",
        paper_source_contract_json=json.dumps(source_contract),
        redis_benchmark_bin="/tmp/redis-benchmark",
        host="127.0.0.1",
        port=6379,
        requests=1000,
        clients=50,
        tests="set,get",
        data_size=64,
        threads=0,
    )


class RedisBenchmarkJsonClaimContractTests(unittest.TestCase):
    def accepted_source_contract(self) -> dict:
        return {
            "accepted_newer_checkout_pin_complete": True,
            "source_provenance_class": "user_accepted_newer_pinned_source",
            "checkout_pin": {
                "pinned": True,
                "fallback_used": True,
                "non_exact_ref_used": True,
                "commit": "0123456789abcdef0123456789abcdef01234567",
            },
        }

    def test_accepted_current_unialloc_contract_can_mark_runtime_claim_grade(self) -> None:
        contract = wrapper.current_source_claim_contract(
            args_for("unialloc", self.accepted_source_contract()),
            allocator_semantics={"claim_grade_blockers": []},
            benchmark_path="/tmp/redis-benchmark",
        )

        self.assertTrue(contract["claim_grade"], contract)
        self.assertTrue(contract["accepted_current_source"], contract)
        self.assertFalse(contract["paper_exact"], contract)
        self.assertEqual(contract["claim_grade_blockers"], [])


    def test_common_payload_stays_fail_closed_until_benchmark_rows_succeed(self) -> None:
        payload = wrapper.build_common_payload(
            args_for("unialloc", self.accepted_source_contract()),
            server_command=["cargo", "run"],
            cwd=str(ROOT),
            benchmark_path="/tmp/redis-benchmark",
        )

        self.assertTrue(payload["claim_grade_contract_preconditions_complete"], payload)
        self.assertFalse(payload["claim_grade"], payload)
        self.assertIn("has not completed successfully", " ".join(payload["claim_grade_blockers"]))

    def test_success_promotion_requires_success_and_parseable_operations(self) -> None:
        payload = wrapper.build_common_payload(
            args_for("unialloc", self.accepted_source_contract()),
            server_command=["cargo", "run"],
            cwd=str(ROOT),
            benchmark_path="/tmp/redis-benchmark",
        )
        payload.update({"success": False, "benchmark_count": 1, "operation_count": 100})
        wrapper.promote_successful_claim_grade(payload)
        self.assertFalse(payload["claim_grade"], payload)

        payload.update({"success": True, "benchmark_count": 1, "operation_count": 100})
        wrapper.promote_successful_claim_grade(payload)
        self.assertTrue(payload["claim_grade"], payload)
        self.assertEqual(payload["claim_grade_blockers"], [])

    def test_allocator_semantics_blockers_keep_runtime_claim_fail_closed(self) -> None:
        contract = wrapper.current_source_claim_contract(
            args_for("scudo", self.accepted_source_contract()),
            allocator_semantics={
                "claim_grade_blockers": [
                    "Scudo server runtime identity has not been verified before workload timing",
                ]
            },
            benchmark_path="/tmp/redis-benchmark",
        )

        self.assertFalse(contract["claim_grade"], contract)
        self.assertIn("runtime identity", " ".join(contract["claim_grade_blockers"]))

    def test_missing_accepted_source_contract_keeps_runtime_claim_fail_closed(self) -> None:
        contract = wrapper.current_source_claim_contract(
            args_for("unialloc", {}),
            allocator_semantics={"claim_grade_blockers": []},
            benchmark_path="/tmp/redis-benchmark",
        )

        self.assertFalse(contract["claim_grade"], contract)
        self.assertIn("accepted pinned", " ".join(contract["claim_grade_blockers"]))


if __name__ == "__main__":
    unittest.main()
