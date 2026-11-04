"""OrcaSlicer CLI backend ("Orca compatibility" from the README).

ARTIFEX's AI-chosen settings are translated into Orca process keys and written to a
temporary process profile that inherits from the configured base profile.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import SlicerConfig
from .engine import SliceError, SliceResult
from .settings import SliceSettings

PATTERNS = {"grid": "grid", "lines": "line", "triangles": "triangles"}


def orca_process_overrides(s: SliceSettings) -> dict[str, str]:
    """Orca stores almost every value as a string."""
    return {
        "layer_height": f"{s.layer_height:.2f}",
        "initial_layer_print_height": f"{s.first_layer_height:.2f}",
        "wall_loops": str(s.perimeters),
        "top_shell_layers": str(s.top_layers),
        "bottom_shell_layers": str(s.bottom_layers),
        "sparse_infill_density": f"{s.infill_density * 100:.0f}%",
        "sparse_infill_pattern": PATTERNS.get(s.infill_pattern, "grid"),
        "enable_support": "1" if s.supports else "0",
        "support_type": "tree(auto)",
        "support_threshold_angle": str(int(90 - s.support_angle)),
        "brim_type": "outer_only" if s.brim_width > 0 else "no_brim",
        "brim_width": f"{s.brim_width:.1f}",
        "skirt_loops": str(s.skirt_loops),
        "outer_wall_speed": f"{s.outer_perimeter_speed * s.speed_factor:.0f}",
        "inner_wall_speed": f"{s.perimeter_speed * s.speed_factor:.0f}",
        "sparse_infill_speed": f"{s.infill_speed * s.speed_factor:.0f}",
        "internal_solid_infill_speed": f"{s.solid_infill_speed * s.speed_factor:.0f}",
    }


def _parse_time(text: str) -> float:
    total = 0.0
    for val, unit in re.findall(r"(\d+)\s*([dhms])", text):
        total += int(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


class OrcaSlicer:
    name = "orca"

    def __init__(self, cfg: SlicerConfig):
        self.cfg = cfg

    def available(self) -> bool:
        return shutil.which(self.cfg.orca_command) is not None

    def slice(self, stl_path: Path, out_path: Path, s: SliceSettings, progress=None) -> SliceResult:
        if not self.available():
            raise SliceError(f"OrcaSlicer '{self.cfg.orca_command}' nicht gefunden")
        if not (self.cfg.orca_machine_profile and self.cfg.orca_process_profile and self.cfg.orca_filament_profile):
            raise SliceError("Orca-Profile (machine/process/filament) sind nicht konfiguriert")
        if progress:
            progress("OrcaSlicer läuft", 0.1)
        with tempfile.TemporaryDirectory(prefix="artifex-orca-") as tmp:
            tmpdir = Path(tmp)
            process = json.loads(Path(self.cfg.orca_process_profile).read_text(encoding="utf-8"))
            process.update(orca_process_overrides(s))
            process_path = tmpdir / "process.json"
            process_path.write_text(json.dumps(process), encoding="utf-8")
            filament = json.loads(Path(self.cfg.orca_filament_profile).read_text(encoding="utf-8"))
            filament["nozzle_temperature"] = [str(int(s.nozzle_temp))]
            filament["hot_plate_temp"] = [str(int(s.bed_temp))]
            filament_path = tmpdir / "filament.json"
            filament_path.write_text(json.dumps(filament), encoding="utf-8")
            cmd = [
                self.cfg.orca_command,
                "--slice", "0",
                "--load-settings", f"{self.cfg.orca_machine_profile};{process_path}",
                "--load-filaments", str(filament_path),
                "--outputdir", str(tmpdir),
                str(stl_path),
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cfg.orca_timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise SliceError("OrcaSlicer Zeitüberschreitung") from exc
            gcodes = sorted(tmpdir.glob("*.gcode"))
            if proc.returncode != 0 or not gcodes:
                raise SliceError(f"OrcaSlicer fehlgeschlagen: {(proc.stderr or proc.stdout)[-500:]}")
            shutil.move(str(gcodes[0]), out_path)

        text = out_path.read_text(encoding="utf-8", errors="ignore")
        est = re.search(r"estimated printing time.*?=\s*(.+)", text)
        fil_mm = re.search(r"filament used \[mm\]\s*=\s*([\d.]+)", text)
        fil_g = re.search(r"filament used \[g\]\s*=\s*([\d.]+)", text)
        layers = len(re.findall(r"^;LAYER_CHANGE", text, re.M))
        max_z = re.findall(r"^;Z:([\d.]+)", text, re.M)
        if progress:
            progress("Fertig geslict", 1.0)
        return SliceResult(
            gcode_path=out_path,
            layers=layers,
            estimated_time_s=_parse_time(est.group(1)) if est else 0.0,
            filament_mm=float(fil_mm.group(1)) if fil_mm else 0.0,
            filament_g=float(fil_g.group(1)) if fil_g else 0.0,
            max_z=float(max_z[-1]) if max_z else 0.0,
            slicer="OrcaSlicer",
        )
