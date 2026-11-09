"""Sync the standalone client into the self-contained Home Assistant integration."""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packages/scpi_psu_client/scpi_psu_client"
VENDOR = ROOT / "custom_components/scpi_psu/scpi_psu_client"


def files(directory: Path) -> set[Path]:
    return {
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the vendored client differs")
    args = parser.parse_args()
    if not (SOURCE / "__init__.py").is_file():
        parser.error(f"Canonical client source is missing or incomplete: {SOURCE}")
    source_files = files(SOURCE)
    vendor_files = files(VENDOR)
    different = (source_files ^ vendor_files) | {
        path
        for path in source_files & vendor_files
        if not filecmp.cmp(SOURCE / path, VENDOR / path, shallow=False)
    }
    if args.check:
        if different:
            print("Vendored client differs; run python scripts/sync_client.py:")
            for path in sorted(different):
                print(f"  {path}")
            return 1
        return 0
    for path in vendor_files - source_files:
        (VENDOR / path).unlink()
    for path in source_files:
        destination = VENDOR / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / path, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
