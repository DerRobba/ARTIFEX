"""Built-in parametric OpenSCAD templates.

They are the offline fallback when no LLM is reachable (e.g. at a fair without internet)
and a safety net when AI code generation fails for a simple, common object.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class Template:
    name: str
    title: str
    file: str
    keywords: tuple[str, ...]
    # Maps the requested largest dimension (mm) to template parameters.
    sizer: Callable[[float], dict[str, Any]]
    default_size: float
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return TEMPLATE_DIR / self.file


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in [
        Template(
            "keychain", "Schlüsselanhänger", "sign.scad",
            ("schlüsselanhänger", "schluesselanhaenger", "keychain", "key tag", "anhänger", "namensschild"),
            lambda s: {"width": s, "height": round(s * 0.36, 1), "thickness": 3, "hole_d": 4.5, "corner_r": 4},
            50,
        ),
        Template(
            "sign", "Schild", "sign.scad",
            ("schild", "sign", "plate", "türschild", "tafel", "beschriftung", "text"),
            lambda s: {"width": s, "height": round(s * 0.3, 1), "thickness": 3},
            100,
        ),
        Template(
            "box", "Box", "box.scad",
            ("box", "kiste", "schachtel", "behälter", "behaelter", "container", "organizer", "dose", "kästchen"),
            lambda s: {"width": s, "depth": round(s * 0.7, 1), "height": round(s * 0.5, 1)},
            60,
        ),
        Template(
            "cup", "Becher", "cup.scad",
            ("becher", "tasse", "cup", "mug", "stiftehalter", "pen holder", "stifthalter"),
            lambda s: {"height": s, "diameter": round(s * 0.75, 1)},
            90,
        ),
        Template(
            "vase", "Vase", "vase.scad",
            ("vase", "blumentopf", "übertopf"),
            lambda s: {"height": s, "diameter": round(s * 0.55, 1)},
            120,
        ),
        Template(
            "hook", "Wandhaken", "hook.scad",
            ("haken", "hook", "aufhänger", "kleiderhaken"),
            lambda s: {"plate_h": s, "hook_depth": round(s * 0.5, 1), "width": max(12, round(s * 0.33, 1))},
            60,
        ),
        Template(
            "phone_stand", "Handyhalter", "phone_stand.scad",
            ("handyhalter", "handyständer", "handy-ständer", "phone stand", "smartphone", "tablet", "handy"),
            lambda s: {"base_depth": s, "width": round(s * 0.8, 1)},
            90,
        ),
    ]
}

_QUOTED = re.compile(r"[\"„“'‚‘»«]([^\"„“'‚‘»«]{1,40})[\"“”'‘’«»]")


def match_template(prompt: str) -> Template | None:
    p = prompt.lower()
    for t in TEMPLATES.values():
        # Plain substring match on purpose: German compounds ("Wandhaken", "Stiftebecher").
        if any(k in p for k in t.keywords):
            return t
    return None


def extract_label(prompt: str) -> str | None:
    m = _QUOTED.search(prompt)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:mit (?:dem )?(?:text|namen|aufschrift|schrift)|with (?:the )?(?:text|name))\s*:?\s*(\S+(?:\s\S+)?)", prompt, re.I)
    if m:
        return m.group(1).strip(" .,!")
    return None


def template_params(t: Template, size_mm: float, prompt: str) -> dict[str, Any]:
    params = dict(t.defaults)
    params.update(t.sizer(size_mm))
    if t.file == "sign.scad":
        params["label"] = extract_label(prompt) or "ARTIFEX"
    return params


def scad_value(v: Any) -> str:
    """Format a Python value as an OpenSCAD literal for ``-D name=value``."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'
