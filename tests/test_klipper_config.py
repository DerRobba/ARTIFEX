"""Static checks of the Klipper configuration + optional real Klipper batch run.

Set KLIPPER_DIR (klipper checkout with a built out/klipper.dict for rp2040) and
KLIPPY_PYTHON to run the configuration and real slicer output through klippy.
"""
import configparser
import os
import re
import subprocess
from pathlib import Path

import pytest
import trimesh

from artifex.slicer import ArtifexSlicer, SliceSettings

ROOT = Path(__file__).resolve().parents[1]
KLIPPER = ROOT / "klipper"


def _sections(path: Path) -> list[str]:
    return re.findall(r"^\[([^\]]+)\]", path.read_text(encoding="utf-8"), re.M)


def test_includes_exist():
    text = (KLIPPER / "printer.cfg").read_text()
    for inc in re.findall(r"^#?\s*\[include ([^\]]+)\]", text, re.M):
        assert (KLIPPER / inc).is_file(), inc


def test_config_files_parse():
    for f in [KLIPPER / "printer.cfg", *sorted((KLIPPER / "artifex").glob("*.cfg"))]:
        cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=("#", ";"))
        cp.read_string(f.read_text(encoding="utf-8"))
        assert cp.sections(), f


def test_macros_used_by_host_exist():
    macros = set()
    for f in (KLIPPER / "artifex").glob("*.cfg"):
        macros |= {s.split(" ", 1)[1].upper() for s in _sections(f) if s.startswith("gcode_macro ")}
    used = {"PRINT_START", "PRINT_END", "AFS_CHANGE", "AFS_LOAD", "AFS_UNLOAD", "AFS_LOAD_CONFIRM",
            "TOOL_CHANGE", "TOOL_CONFIRM", "FILTER_ON", "FILTER_OFF", "_ARTIFEX_PARK", "M600"}
    assert used <= macros, used - macros


@pytest.mark.skipif(not os.environ.get("KLIPPER_DIR"), reason="set KLIPPER_DIR to run klippy batch validation")
def test_klippy_batch(tmp_path):
    kdir = Path(os.environ["KLIPPER_DIR"])
    py = os.environ.get("KLIPPY_PYTHON", "python3")
    home = tmp_path / "home"
    cfg_dir = home / "printer_data" / "config"
    cfg_dir.mkdir(parents=True)
    (home / "printer_data" / "gcodes").mkdir()
    (cfg_dir / "artifex").symlink_to(KLIPPER / "artifex")
    (cfg_dir / "printer.cfg").write_text((KLIPPER / "printer.cfg").read_text())

    cube = trimesh.creation.box((20, 20, 6))
    cube.apply_translation((117.5, 117.5, 3))
    cube.export(tmp_path / "c.stl")
    ArtifexSlicer().slice(tmp_path / "c.stl", tmp_path / "c.gcode", SliceSettings(afs_slot=1))
    gcode = tmp_path / "test.gcode"
    gcode.write_text("G28\n" + (tmp_path / "c.gcode").read_text() + "\nAFS_LOAD_CONFIRM\nTOOL_CHANGE TOOL=pen\nTOOL_CONFIRM\n")
    log = tmp_path / "klippy.log"
    subprocess.run(
        [py, str(kdir / "klippy" / "klippy.py"), str(cfg_dir / "printer.cfg"), "-i", str(gcode),
         "-o", str(tmp_path / "out.serial"), "-v", "-d", str(kdir / "out" / "klipper.dict"), "-l", str(log)],
        env={**os.environ, "HOME": str(home)}, timeout=600, check=False,
    )
    text = log.read_text()
    assert "Exiting (print time" in text
    assert "Error evaluating" not in text and "Move out of range" not in text
