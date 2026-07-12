#!/usr/bin/env python3
"""Unit tests for the bounded current-source Redox contract checker."""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "redox_current_source_contract.py"
SPEC = importlib.util.spec_from_file_location("redox_current_source_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


class RedoxCurrentSourceContractTests(unittest.TestCase):
    def test_symbol_contract_is_exact_and_fail_closed(self) -> None:
        complete = "\n".join(
            f"0000000000000000 T {symbol}" for symbol in contract.REQUIRED_C_ABI_SYMBOLS
        )
        symbols = contract.symbols_from_nm(complete)
        self.assertTrue(contract.symbol_contract(symbols)["passed"])
        symbols.remove("unialloc_realloc")
        audit = contract.symbol_contract(symbols)
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["missing"], ["unialloc_realloc"])

    def test_elf_contract_requires_x86_64_relocatable_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "unialloc-redox.o"
            header = bytearray(20)
            header[:4] = b"\x7fELF"
            header[4] = 2
            header[5] = 1
            header[16:18] = (1).to_bytes(2, "little")
            header[18:20] = (62).to_bytes(2, "little")
            path.write_bytes(header)
            self.assertTrue(contract.elf_x86_64_relocatable_contract(path)["passed"])
            header[18:20] = (183).to_bytes(2, "little")
            path.write_bytes(header)
            self.assertFalse(contract.elf_x86_64_relocatable_contract(path)["passed"])


if __name__ == "__main__":
    unittest.main()
