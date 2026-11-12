"""2D geometry helpers for the slicer (shapely based)."""
from __future__ import annotations

import math
from typing import Iterable, Iterator

import numpy as np
from shapely import affinity
from shapely.geometry import GeometryCollection, LinearRing, LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

EMPTY = GeometryCollection()


def polygons(geom: BaseGeometry | None) -> list[Polygon]:
    """Flatten any geometry into a list of non-empty polygons."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(polygons(g))
        return out
    return []


def lines(geom: BaseGeometry | None) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, (MultiLineString, GeometryCollection)):
        out: list[LineString] = []
        for g in geom.geoms:
            out.extend(lines(g))
        return out
    return []


def clean(geom: BaseGeometry | None, simplify: float = 0.01) -> BaseGeometry:
    if geom is None or geom.is_empty:
        return EMPTY
    if not geom.is_valid:
        geom = make_valid(geom)
    geom = unary_union(polygons(geom))
    if simplify > 0:
        geom = geom.simplify(simplify, preserve_topology=True)
    return geom


def offset(geom: BaseGeometry, distance: float) -> BaseGeometry:
    if geom.is_empty:
        return EMPTY
    return geom.buffer(distance, join_style="mitre", mitre_limit=2.0, quad_segs=4)


def union(geoms: Iterable[BaseGeometry]) -> BaseGeometry:
    items = [g for g in geoms if g is not None and not g.is_empty]
    return unary_union(items) if items else EMPTY


def intersection_all(geoms: list[BaseGeometry]) -> BaseGeometry:
    if not geoms:
        return EMPTY
    acc = geoms[0]
    for g in geoms[1:]:
        if acc.is_empty:
            return EMPTY
        acc = acc.intersection(g)
    return acc


def remove_small(geom: BaseGeometry, min_area: float) -> BaseGeometry:
    return union(p for p in polygons(geom) if p.area >= min_area)


def rings(geom: BaseGeometry) -> Iterator[tuple[LinearRing, bool]]:
    """Yield (ring, is_hole) for every polygon boundary."""
    for p in polygons(geom):
        yield p.exterior, False
        for interior in p.interiors:
            yield interior, True


def ring_points(ring: LinearRing, start_near: tuple[float, float] | None = None) -> np.ndarray:
    """Closed ring as (N+1, 2) array, rotated to start at the vertex nearest ``start_near``."""
    pts = np.asarray(ring.coords)[:-1]
    if len(pts) == 0:
        return pts
    if start_near is not None:
        d = np.hypot(pts[:, 0] - start_near[0], pts[:, 1] - start_near[1])
        k = int(np.argmin(d))
        pts = np.roll(pts, -k, axis=0)
    return np.vstack([pts, pts[:1]])


def hatch(region: BaseGeometry, spacing: float, angle_deg: float, min_length: float = 0.2) -> list[np.ndarray]:
    """Parallel fill lines (anchored to the global origin so layers line up)."""
    if region.is_empty or spacing <= 0:
        return []
    rot = affinity.rotate(region, -angle_deg, origin=(0, 0))
    minx, miny, maxx, maxy = rot.bounds
    k0 = math.floor(miny / spacing)
    k1 = math.ceil(maxy / spacing)
    if k1 - k0 > 20000:
        return []
    segs = [((minx - 1, k * spacing + spacing / 2), (maxx + 1, k * spacing + spacing / 2)) for k in range(k0, k1 + 1)]
    clipped = rot.intersection(MultiLineString(segs))
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    out: list[np.ndarray] = []
    for ln in lines(clipped):
        if ln.length < min_length:
            continue
        pts = np.asarray(ln.coords)
        x = pts[:, 0] * c - pts[:, 1] * s
        y = pts[:, 0] * s + pts[:, 1] * c
        out.append(np.column_stack([x, y]))
    return out


def order_paths(paths: list[np.ndarray], start: tuple[float, float]) -> list[np.ndarray]:
    """Greedy nearest-neighbour ordering of open polylines (each may be reversed)."""
    if len(paths) <= 1:
        return list(paths)
    starts = np.array([p[0] for p in paths])
    ends = np.array([p[-1] for p in paths])
    used = np.zeros(len(paths), dtype=bool)
    pos = np.asarray(start, dtype=float)
    out: list[np.ndarray] = []
    for _ in range(len(paths)):
        ds = np.hypot(*(starts - pos).T)
        de = np.hypot(*(ends - pos).T)
        ds[used] = np.inf
        de[used] = np.inf
        i_s, i_e = int(np.argmin(ds)), int(np.argmin(de))
        if ds[i_s] <= de[i_e]:
            i, path = i_s, paths[i_s]
        else:
            i, path = i_e, paths[i_e][::-1]
        used[i] = True
        out.append(path)
        pos = path[-1]
    return out
