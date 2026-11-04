import trimesh

from artifex.llm import LLMClient
from artifex.pipeline.parametric import ParametricError, ParametricGenerator, check_code_safety
from artifex.pipeline.plan import DesignPlan
from artifex.pipeline.templates import TEMPLATES, match_template, template_params

from .conftest import needs_openscad


def test_code_safety():
    assert check_code_safety("cube(10);", []) == []
    assert check_code_safety('import("/etc/passwd");', [])
    assert check_code_safety("include </home/pi/secret.scad>", [])
    assert check_code_safety("use <BOSL2/std.scad>", []) != []
    assert check_code_safety("use <BOSL2/std.scad>", ["BOSL2/"]) == []
    assert check_code_safety("use <BOSL2/../../x.scad>", ["BOSL2/"]) != []


def test_template_matching():
    assert match_template("Ein Becher für Stifte").name == "cup"
    assert match_template("Schlüsselanhänger").name == "keychain"
    assert match_template("Drache") is None


@needs_openscad
def test_all_templates_render(cfg, tmp_path):
    gen = ParametricGenerator(cfg, LLMClient(cfg.llm))
    for name, t in TEMPLATES.items():
        plan = DesignPlan(refined_prompt=name, mode="parametric", template=name,
                          template_params=template_params(t, t.default_size, '"Test"'))
        res = gen.generate(plan, tmp_path / name)
        mesh = trimesh.load(res.stl_path)
        assert mesh.is_watertight, name
        assert max(mesh.extents) > 10, name
        assert mesh.bounds[0][2] >= -1e-3, name


@needs_openscad
def test_llm_codegen_repair_loop(cfg, fake_llm, tmp_path):
    answers = iter([
        "```openscad\ncube([20, 20, 10]\n```",          # syntax error
        '```openscad\nimport("x.stl");\n```',           # forbidden
        "```openscad\nsize = 20;\ntranslate([-size/2, -size/2, 0]) cube([size, size, 10]);\n```",
    ])
    fake_llm.handlers["OpenSCAD engineer"] = lambda msgs: next(answers)
    gen = ParametricGenerator(cfg, fake_llm.client(cfg))
    plan = DesignPlan(refined_prompt="a 20 mm block", mode="parametric", size_mm=20)
    res = gen.generate(plan, tmp_path)
    assert res.attempts == 3
    # the compiler error was fed back to the model
    last_call = fake_llm.calls[-1]["messages"]
    assert any("OpenSCAD reported a problem" in m["content"] for m in last_call if m["role"] == "user")
    mesh = trimesh.load(res.stl_path)
    assert abs(mesh.extents[0] - 20) < 1e-3


@needs_openscad
def test_codegen_falls_back_to_template(cfg, fake_llm, tmp_path):
    fake_llm.handlers["OpenSCAD engineer"] = lambda msgs: "```openscad\nthis is not scad\n```"
    cfg.openscad.max_attempts = 2
    gen = ParametricGenerator(cfg, fake_llm.client(cfg))
    plan = DesignPlan(refined_prompt="cup", mode="parametric", template="cup",
                      template_params=template_params(TEMPLATES["cup"], 60, "cup"))
    res = gen.generate(plan, tmp_path)
    assert res.used_template == "cup"


def test_codegen_without_llm_or_template_fails(cfg, tmp_path):
    gen = ParametricGenerator(cfg, LLMClient(cfg.llm))
    plan = DesignPlan(refined_prompt="something odd", mode="parametric")
    try:
        gen.generate(plan, tmp_path)
    except ParametricError:
        pass
    else:
        raise AssertionError("expected ParametricError")
