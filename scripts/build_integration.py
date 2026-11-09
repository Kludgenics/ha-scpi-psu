"""Build a self-contained Home Assistant integration archive."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts/sync_client.py")], check=True)
    destination = ROOT / "dist/scpi_psu"
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.with_suffix(".zip")
    component = ROOT / "custom_components/scpi_psu"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path in sorted(component.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                bundle.write(path, path.relative_to(component.parent))
    print(f"Built {archive}")


if __name__ == "__main__":
    main()
