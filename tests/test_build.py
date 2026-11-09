"""Build failures must preserve the checked-in integration."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("empty_source", [False, True])
def test_sync_without_canonical_source_preserves_vendor(tmp_path: Path, empty_source: bool) -> None:
    script = tmp_path / "scripts/sync_client.py"
    script.parent.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "scripts/sync_client.py", script)
    if empty_source:
        (tmp_path / "packages/scpi_psu_client/scpi_psu_client").mkdir(parents=True)
    vendor = tmp_path / "custom_components/scpi_psu/scpi_psu_client/client.py"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("preserve this client")

    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "Canonical client source is missing or incomplete" in result.stderr
    assert vendor.read_text() == "preserve this client"
