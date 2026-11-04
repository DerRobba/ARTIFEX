import json

import pytest

from artifex.llm import LLMError, extract_code, extract_json
from artifex.pipeline.plan import JobOptions
from artifex.pipeline.router import Router, heuristic_plan, parse_dims_mm, parse_size_mm
from artifex.llm import LLMClient


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n{"mode": "organic"}\n```') == {"mode": "organic"}
    assert extract_json('blah {"x": {"y": "}"}} trailing') == {"x": {"y": "}"}}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_extract_code_prefers_openscad_block():
    text = "Here:\n```python\nprint(1)\n```\n```openscad\ncube(10);\n```"
    assert extract_code(text) == "cube(10);"
    assert extract_code("cube(5);") == "cube(5);"


def test_size_parsing():
    assert parse_size_mm("eine 10 cm hohe Eule") == 100
    assert parse_size_mm("Halter 2,5 cm") == 25
    assert parse_dims_mm("Box 80 x 50 x 30 mm") == {"x": 80, "y": 50, "z": 30}
    assert parse_dims_mm("Box 8×5×3 cm") == {"x": 80, "y": 50, "z": 30}
    assert parse_size_mm("ohne Größe") is None


def test_heuristic_routing(cfg):
    owl = heuristic_plan("kleine Eule auf einem Ast", JobOptions(), cfg)
    assert owl.mode == "organic" and owl.image_prompt and "white background" in owl.image_prompt

    tag = heuristic_plan('Schlüsselanhänger mit dem Text "LEO"', JobOptions(), cfg)
    assert tag.mode == "parametric" and tag.template == "keychain"
    assert tag.template_params["label"] == "LEO"

    box = heuristic_plan("Box 80 x 50 x 30 mm", JobOptions(), cfg)
    assert box.template == "box"
    assert (box.template_params["width"], box.template_params["depth"], box.template_params["height"]) == (80, 50, 30)


def test_router_uses_llm_and_user_overrides(cfg, fake_llm):
    fake_llm.handlers["design planner"] = lambda msgs: json.dumps({
        "object_name": "Drache", "mode": "organic", "reasoning": "Figur", "refined_prompt": "a cute dragon",
        "size_mm": 500, "functional": False, "material": "pla", "image_prompt": "a cute dragon, white background",
    })
    router = Router(cfg, fake_llm.client(cfg))
    plan = router.plan("Drache", JobOptions())
    assert plan.source == "llm" and plan.mode == "organic" and plan.material == "PLA"
    # 500 mm does not fit -> limited to the build volume
    assert plan.size_mm <= cfg.printer.bed_x - 2 * cfg.printer.margin

    plan = router.plan("Drache", JobOptions(mode="parametric", size_mm=40))
    assert plan.mode == "parametric" and plan.size_mm == 40 and plan.scad_spec


def test_router_falls_back_when_llm_broken(cfg, fake_llm):
    fake_llm.handlers["design planner"] = lambda msgs: "I am not JSON"
    plan = Router(cfg, fake_llm.client(cfg)).plan("Wandhaken", JobOptions())
    assert plan.source == "heuristic" and plan.template == "hook"


def test_llm_disabled_is_not_available(cfg):
    assert not LLMClient(cfg.llm).available
