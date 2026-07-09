#!/usr/bin/env python3
from pathlib import Path
import sys


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "evaluation" / "scripts" / "paper_external_workload_adapter.py"
        if candidate.exists():
            return parent
    raise RuntimeError("could not locate evaluation/scripts/paper_external_workload_adapter.py")


ROOT = _find_repo_root()
sys.path.insert(0, str(ROOT / "evaluation" / "scripts"))

from paper_external_workload_adapter import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(default_config_dir=Path(__file__).resolve().parent))
