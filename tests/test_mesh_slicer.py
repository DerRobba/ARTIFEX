import re

import numpy as np
import pytest
import trimesh

from artifex.pipeline.mesh import prepare_mesh
from artifex.pipeline.plan import DesignPlan
from artifex.pipeline.settings_ai import rule_based, sanitize_ai_settings
from artifex.pipeline.mesh import MeshStats
from artifex.slicer import ArtifexSlicer, SliceError, SliceSettings
from artifex.slicer.gcode import move_time
from artifex.slicer.orca import orca_process_overrides
from artifex.slicer.settings import ColorChange


def _export(mesh, path):
    mesh.export(path)
    return path


def _layers(gcode: str) -> list[str]:
    return gcode.split(";LAYER_CHANGE")[1:]


def test_prepare_mesh_scales_and_centers(cfg, tmp_path):
    src = _export(trimesh.creation.box((10, 20, 5)), tmp_path / "a.stl")
    stats = prepare_mesh(src, tmp_path / "b.stl", cfg, size_mm=40, organic=False)
    m = trimesh.load(tmp_path / "b.stl")
    assert np.allclose(m.extents, [20, 40, 10], atol=1e-6)
    assert abs(m.bounds[0][2]) < 1e-9
    assert np.allclose((m.bounds[0][:2] + m.bounds[1][:2]) / 2, [cfg.printer.bed_x / 2, cfg.printer.bed_y / 2])
    assert stats.watertight and stats.overhang_ratio == 0


def test_prepare_mesh_fits_build_volume(cfg, tmp_path):
    src = _export(trimesh.creation.box((400, 50, 50)), tmp_path / "big.stl")
    stats = prepare_mesh(src, tmp_path / "out.stl", cfg, size_mm=None, organic=False)
    assert stats.size_x <= cfg.printer.bed_x - 2 * cfg.printer.margin + 1e-6
    assert any("verkleinert" in n for n in stats.repaired)


def test_organic_unstable_pose_is_fixed_and_base_flattened(cfg, tmp_path):
    # A cone standing on its tip is unstable -> must be re-oriented.
    cone = trimesh.creation.cone(radius=10, height=30)
    cone.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    src = _export(cone, tmp_path / "cone.stl")
    stats = prepare_mesh(src, tmp_path / "out.stl", cfg, size_mm=30, organic=True)
    assert stats.base_area_mm2 > 50
    sphere = _export(trimesh.creation.icosphere(subdivisions=3, radius=10), tmp_path / "s.stl")
    stats = prepare_mesh(sphere, tmp_path / "s2.stl", cfg, size_mm=20, organic=True)
    assert stats.base_area_mm2 > 1.0  # flat contact patch
    assert any("abgeflacht" in n for n in stats.repaired)


def test_slice_cube(tmp_path):
    cube = trimesh.creation.box((20, 20, 10))
    cube.apply_translation((117.5, 117.5, 5))
    src = _export(cube, tmp_path / "cube.stl")
    s = SliceSettings(layer_height=0.2, first_layer_height=0.2, infill_density=0.2)
    r = ArtifexSlicer().slice(src, tmp_path / "cube.gcode", s)
    g = (tmp_path / "cube.gcode").read_text()
    assert r.layers == 50
    assert "PRINT_START BED=60 EXTRUDER=210 SLOT=0" in g and "PRINT_END" in g
    assert "M83" in g and "SET_PRINT_STATS_INFO TOTAL_LAYER=50" in g
    layers = _layers(g)
    assert ";TYPE:Solid infill" in layers[1] and ";TYPE:Sparse infill" not in layers[1]
    assert ";TYPE:Sparse infill" in layers[25]
    assert ";TYPE:Solid infill" in layers[-1]
    xs = [float(x) for x in re.findall(r"X([\d.]+)", g)]
    ys = [float(y) for y in re.findall(r"Y([\d.]+)", g)]
    assert min(xs) > 100 and max(xs) < 135 and min(ys) > 100 and max(ys) < 135
    # material estimate ~ volume * (perimeters + infill) is plausible: 4 cm3 cube
    assert 1.5 < r.filament_g < 5.0
    assert r.estimated_time_s > 180


def test_slice_supports_and_color_change(tmp_path):
    stem = trimesh.creation.box((10, 10, 20))
    stem.apply_translation((117.5, 117.5, 10))
    top = trimesh.creation.box((40, 10, 4))
    top.apply_translation((117.5, 117.5, 22))
    t = trimesh.util.concatenate([stem, top])
    try:
        t = trimesh.boolean.union([stem, top])
    except Exception:  # noqa: BLE001 - no boolean backend
        pytest.skip("no boolean backend")
    src = _export(t, tmp_path / "t.stl")
    s = SliceSettings(supports=True, color_changes=[ColorChange(z=12.0, slot=2)])
    ArtifexSlicer().slice(src, tmp_path / "t.gcode", s)
    g = (tmp_path / "t.gcode").read_text()
    assert ";TYPE:Support" in g
    assert g.count("AFS_CHANGE SLOT=2") == 1
    no_sup = ArtifexSlicer().slice(src, tmp_path / "t2.gcode", SliceSettings())
    assert ";TYPE:Support" not in (tmp_path / "t2.gcode").read_text()
    assert no_sup.layers > 0


def test_slice_rejects_model_outside_bed(tmp_path):
    cube = trimesh.creation.box((20, 20, 10))
    cube.apply_translation((5, 5, 5))
    with pytest.raises(SliceError):
        ArtifexSlicer().slice(_export(cube, tmp_path / "c.stl"), tmp_path / "c.gcode", SliceSettings())


def test_move_time_model():
    assert move_time(100, 100, 3000, 0) > 1.0  # needs acceleration time
    assert move_time(0.1, 100, 3000, 0) < 0.02
    assert abs(move_time(1000, 100, 1e9, 100) - 10) < 1e-6


def test_settings_overrides_are_clamped():
    s = SliceSettings.for_material("petg").with_overrides({"layer_height": 5, "perimeters": 0.4, "infill_density": 3})
    assert s.material == "PETG" and s.nozzle_temp == 240
    assert s.layer_height == 0.3 and s.perimeters == 1 and s.infill_density == 1.0
    ai = sanitize_ai_settings({"infill_density": "25%", "supports": "true", "layer_height": 0.01, "infill_pattern": "honeycomb"})
    assert ai == {"infill_density": 0.25, "supports": True, "layer_height": 0.06}


def test_rule_based_settings():
    stats = MeshStats(size_x=20, size_y=20, size_z=80, volume_cm3=10, faces=100, watertight=True,
                      overhang_ratio=0.1, base_area_mm2=10, slenderness=4, repaired=[])
    plan = DesignPlan(refined_prompt="figure", mode="organic")
    values, why = rule_based(plan, stats, SliceSettings())
    assert values["supports"] is True and values["brim_width"] > 0 and values["layer_height"] < 0.2
    assert why


def test_orca_mapping():
    o = orca_process_overrides(SliceSettings(infill_density=0.2, supports=True, perimeters=3))
    assert o["sparse_infill_density"] == "20%" and o["enable_support"] == "1" and o["wall_loops"] == "3"
