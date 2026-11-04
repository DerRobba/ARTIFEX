"""Mesh post-processing: repair, scale, orient, flatten the base and place on the bed."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import trimesh

from ..config import ArtifexConfig

log = logging.getLogger(__name__)

OVERHANG_COS = np.cos(np.radians(45))  # faces steeper than 45 deg from vertical need support


class MeshError(RuntimeError):
    pass


@dataclass
class MeshStats:
    size_x: float
    size_y: float
    size_z: float
    volume_cm3: float
    faces: int
    watertight: bool
    overhang_ratio: float  # overhanging area / total area
    base_area_mm2: float  # area touching the bed
    slenderness: float  # height / smallest footprint side
    repaired: list[str]

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def load_mesh(path: Path) -> trimesh.Trimesh:
    try:
        loaded = trimesh.load(path, force="mesh")
    except Exception as exc:  # noqa: BLE001
        raise MeshError(f"Mesh konnte nicht geladen werden: {exc}") from exc
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise MeshError("Mesh ist leer")
    return loaded


def repair(mesh: trimesh.Trimesh, notes: list[str]) -> trimesh.Trimesh:
    mesh = mesh.copy()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()

    # Drop small floating fragments (typical for image-to-3D output).
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        areas = np.array([p.area for p in parts])
        keep = [p for p, a in zip(parts, areas) if a >= 0.02 * areas.max()]
        if len(keep) < len(parts):
            notes.append(f"{len(parts) - len(keep)} lose Fragmente entfernt")
            mesh = trimesh.util.concatenate(keep)

    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    trimesh.repair.fix_normals(mesh)
    if not mesh.is_watertight:
        remeshed = _voxel_remesh(mesh)
        if remeshed is not None:
            notes.append("Mesh war nicht geschlossen - per Voxel-Remesh repariert")
            mesh = remeshed
        else:
            notes.append("Mesh ist nicht vollständig geschlossen")
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def _voxel_remesh(mesh: trimesh.Trimesh, cells: int = 160) -> trimesh.Trimesh | None:
    try:
        pitch = float(max(mesh.extents)) / cells
        out = mesh.voxelized(pitch).fill().marching_cubes
        if out.is_empty:
            return None
        trimesh.repair.fix_normals(out)
        return out
    except Exception as exc:  # noqa: BLE001 - scikit-image missing or degenerate input
        log.warning("Voxel remesh failed: %s", exc)
        return None


def decimate(mesh: trimesh.Trimesh, max_faces: int, notes: list[str]) -> trimesh.Trimesh:
    if len(mesh.faces) <= max_faces:
        return mesh
    try:
        out = mesh.simplify_quadric_decimation(face_count=max_faces)
        notes.append(f"Mesh von {len(mesh.faces)} auf {len(out.faces)} Dreiecke reduziert")
        return out
    except Exception as exc:  # noqa: BLE001 - fast_simplification not installed
        log.warning("Decimation unavailable: %s", exc)
        return mesh


def _overhang_area(mesh: trimesh.Trimesh) -> float:
    zmin = mesh.bounds[0][2]
    down = mesh.face_normals[:, 2] < -OVERHANG_COS
    on_bed = mesh.triangles_center[:, 2] < zmin + 0.3
    return float(mesh.area_faces[down & ~on_bed].sum())


def _base_area(mesh: trimesh.Trimesh) -> float:
    zmin = mesh.bounds[0][2]
    flat = (mesh.face_normals[:, 2] < -0.985) & (mesh.triangles_center[:, 2] < zmin + 0.3)
    return float(mesh.area_faces[flat].sum())


def _is_upright_stable(mesh: trimesh.Trimesh) -> bool:
    """Center of mass must project inside the convex hull of the contact points."""
    from shapely.geometry import MultiPoint, Point

    zmin = mesh.bounds[0][2]
    pts = mesh.vertices[mesh.vertices[:, 2] < zmin + max(0.6, 0.01 * mesh.extents[2])][:, :2]
    if len(pts) < 3:
        return False
    hull = MultiPoint([tuple(p) for p in pts]).convex_hull
    com = mesh.center_mass if mesh.is_watertight else mesh.centroid
    return hull.buffer(-0.5).contains(Point(com[0], com[1]))


def orient(mesh: trimesh.Trimesh, notes: list[str], keep_upright: bool = True) -> trimesh.Trimesh:
    """Organic meshes arrive upright (Z-up). Keep that if it is stable, otherwise choose the
    stable resting pose with the least overhang."""
    if keep_upright and _is_upright_stable(mesh):
        return mesh
    try:
        transforms, probs = mesh.compute_stable_poses(n_samples=1, threshold=0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("Stable pose computation failed: %s", exc)
        return mesh
    best, best_score = None, None
    for T, prob in list(zip(transforms, probs))[:8]:
        cand = mesh.copy()
        cand.apply_transform(T)
        score = _overhang_area(cand) / max(cand.area, 1e-9) - 0.3 * prob - 0.2 * _base_area(cand) / max(cand.area, 1e-9)
        if best_score is None or score < best_score:
            best, best_score = cand, score
    if best is not None:
        notes.append("Ausrichtung automatisch optimiert (stabile Lage, wenig Überhänge)")
        return best
    return mesh


def flatten_base(mesh: trimesh.Trimesh, notes: list[str], max_fraction: float = 0.03) -> trimesh.Trimesh:
    """Cut a thin slice off the bottom so a rounded organic model gets a flat, adhering base."""
    zmin, zmax = mesh.bounds[0][2], mesh.bounds[1][2]
    height = zmax - zmin
    footprint = mesh.extents[0] * mesh.extents[1]
    if _base_area(mesh) >= 0.1 * footprint:
        return mesh
    chosen = None
    for frac in (0.005, 0.01, 0.015, 0.02, max_fraction):
        d = max(frac * height, 0.3)
        sec = mesh.section(plane_origin=[0, 0, zmin + d], plane_normal=[0, 0, 1])
        if sec is None:
            continue
        try:
            planar = sec.to_2D() if hasattr(sec, "to_2D") else sec.to_planar()
            area = sum(p.area for p in planar[0].polygons_full)
        except Exception:  # noqa: BLE001
            continue
        chosen = d
        if area >= 0.1 * footprint:
            break
    if chosen is None:
        return mesh
    try:
        cut = trimesh.intersections.slice_mesh_plane(mesh, plane_normal=[0, 0, 1], plane_origin=[0, 0, zmin + chosen], cap=True)
    except Exception as exc:  # noqa: BLE001 - triangulation backend missing
        log.warning("Base flattening failed: %s", exc)
        return mesh
    if cut.is_empty:
        return mesh
    notes.append(f"Standfläche abgeflacht ({chosen:.1f} mm abgeschnitten)")
    return cut


def scale_to_size(mesh: trimesh.Trimesh, size_mm: float) -> trimesh.Trimesh:
    current = float(max(mesh.extents))
    if current <= 0:
        raise MeshError("Mesh hat keine Ausdehnung")
    mesh.apply_scale(size_mm / current)
    return mesh


def fit_build_volume(mesh: trimesh.Trimesh, cfg: ArtifexConfig, notes: list[str]) -> trimesh.Trimesh:
    p = cfg.printer
    limits = np.array([p.bed_x - 2 * p.margin, p.bed_y - 2 * p.margin, p.max_z])
    ext = mesh.extents
    if np.all(ext <= limits):
        return mesh
    factor = float(np.min(limits / ext))
    mesh.apply_scale(factor * 0.999)
    notes.append(f"Auf Bauraum verkleinert (Faktor {factor:.2f})")
    return mesh


def place_on_bed(mesh: trimesh.Trimesh, cfg: ArtifexConfig) -> trimesh.Trimesh:
    b = mesh.bounds
    center_xy = (b[0][:2] + b[1][:2]) / 2
    mesh.apply_translation([cfg.printer.bed_x / 2 - center_xy[0], cfg.printer.bed_y / 2 - center_xy[1], -b[0][2]])
    return mesh


def analyze(mesh: trimesh.Trimesh, notes: list[str]) -> MeshStats:
    ext = mesh.extents
    footprint_min = max(min(ext[0], ext[1]), 1e-6)
    return MeshStats(
        size_x=float(ext[0]),
        size_y=float(ext[1]),
        size_z=float(ext[2]),
        volume_cm3=float(abs(mesh.volume) / 1000.0) if mesh.is_watertight else float(mesh.convex_hull.volume / 1000.0),
        faces=int(len(mesh.faces)),
        watertight=bool(mesh.is_watertight),
        overhang_ratio=float(_overhang_area(mesh) / max(mesh.area, 1e-9)),
        base_area_mm2=float(_base_area(mesh)),
        slenderness=float(ext[2] / footprint_min),
        repaired=list(notes),
    )


def prepare_mesh(
    src: Path,
    dst: Path,
    cfg: ArtifexConfig,
    *,
    size_mm: float | None,
    organic: bool,
    max_faces: int = 200_000,
) -> MeshStats:
    """Full post-processing chain. Writes the print-ready STL to ``dst``."""
    notes: list[str] = []
    mesh = load_mesh(src)
    mesh = repair(mesh, notes)
    mesh = decimate(mesh, max_faces, notes)
    if organic:
        mesh = orient(mesh, notes)
    if size_mm:
        mesh = scale_to_size(mesh, size_mm)
    if organic:
        mesh = flatten_base(mesh, notes)
    mesh = fit_build_volume(mesh, cfg, notes)
    mesh = place_on_bed(mesh, cfg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(dst)
    return analyze(mesh, notes)
