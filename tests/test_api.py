"""End-to-end tests of the web API with fake LLM, mock GPU server and fake Moonraker."""
import io
import json
import time

import httpx
import pytest
import trimesh
from fastapi.testclient import TestClient

from artifex.llm import LLMClient
from artifex.pipeline.organic import InferenceClient
from artifex.printer.moonraker import Moonraker
from artifex.server import build_services, create_app

from .conftest import needs_openscad


class FakeMoonraker:
    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.state = "standby"
        self.filename = ""

    def transport(self):
        def handle(req: httpx.Request) -> httpx.Response:
            self.requests.append((req.method, req.url.path, req.content))
            p = req.url.path
            if p == "/server/info":
                return httpx.Response(200, json={"result": {"klippy_state": "ready"}})
            if p == "/printer/objects/query":
                return httpx.Response(200, json={"result": {"status": {
                    "print_stats": {"state": self.state, "filename": self.filename, "print_duration": 12.0,
                                    "info": {"current_layer": 3, "total_layer": 20}},
                    "virtual_sdcard": {"progress": 0.15},
                    "extruder": {"temperature": 210.2, "target": 210.0, "power": 0.4},
                    "heater_bed": {"temperature": 60.1, "target": 60.0, "power": 0.2},
                    "gcode_macro _AFS": {"current": 0, "pending": -1, "hardware": False},
                    "gcode_macro _TOOLHEAD": {"current": "fdm", "pending": ""},
                }}})
            if p == "/server/files/upload":
                name = "artifex/" + req.content.split(b'filename="')[1].split(b'"')[0].decode()
                return httpx.Response(201, json={"result": {"item": {"path": name, "root": "gcodes"}}})
            if p == "/printer/print/start":
                self.filename = json.loads(req.content)["filename"]
                self.state = "printing"
                return httpx.Response(200, json={"result": "ok"})
            if p.startswith("/printer/"):
                return httpx.Response(200, json={"result": "ok"})
            return httpx.Response(404, json={"error": {"message": "not found"}})

        return httpx.MockTransport(handle)


@pytest.fixture
def env(cfg, fake_llm):
    import server as inference_server  # inference_server/server.py

    inf_app = inference_server.create_app(inference_server.Models(mock=True, low_vram=False, device="cpu"), None)
    moon = FakeMoonraker()
    svc = build_services(
        cfg,
        llm=LLMClient(cfg.llm),
        inference=InferenceClient(cfg, http=TestClient(inf_app)),
        moonraker=Moonraker(cfg.printer, http=httpx.Client(transport=moon.transport())),
    )
    app = create_app(cfg, svc, poll_printer=False)
    with TestClient(app) as client:
        yield client, svc, moon


def wait_job(client, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("ready", "failed", "cancelled", "printing"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


@needs_openscad
def test_offline_parametric_job_and_print(env):
    client, svc, moon = env
    r = client.post("/api/jobs", json={"prompt": 'Schlüsselanhänger mit dem Text "BO"', "slot": 1})
    assert r.status_code == 200
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "ready", job["error"]
    assert job["plan"]["mode"] == "parametric" and job["used_template"] == "keychain"
    assert [s["state"] for s in job["stages"]] == ["done"] * 5
    assert job["settings"]["afs_slot"] == 1
    for f in ("model.scad", "print.stl", "print.gcode"):
        assert f in job["files"]
        assert client.get(f"/api/jobs/{job['id']}/files/{f}").status_code == 200
    gcode = client.get(f"/api/jobs/{job['id']}/files/print.gcode").text
    assert "SLOT=1" in gcode
    assert client.get(f"/api/jobs/{job['id']}/files/../../etc/passwd").status_code == 404

    # print -> uploaded to Moonraker and started
    r = client.post(f"/api/jobs/{job['id']}/print")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "printing"
    paths = [p for _, p, _ in moon.requests]
    assert "/server/files/upload" in paths and "/printer/print/start" in paths
    # second print while printing is refused
    assert client.post(f"/api/jobs/{job['id']}/print").status_code == 409

    # printer finishes -> job becomes done
    moon.state = "complete"
    svc.jobs.on_printer_status(svc.moonraker.status())
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"


@needs_openscad
def test_reslice_changes_settings_and_size(env):
    client, _, _ = env
    job = wait_job(client, client.post("/api/jobs", json={"prompt": "Becher"}).json()["id"])
    assert job["status"] == "ready"
    r = client.post(f"/api/jobs/{job['id']}/reslice", json={"size_mm": 50, "overrides": {"layer_height": 0.28, "supports": True}})
    assert r.status_code == 200
    job2 = wait_job(client, job["id"])
    assert job2["settings"]["layer_height"] == 0.28 and job2["settings"]["supports"] is True
    assert max(job2["mesh_stats"]["size_x"], job2["mesh_stats"]["size_y"], job2["mesh_stats"]["size_z"]) == pytest.approx(50, abs=0.01)


def test_organic_job_with_mock_gpu_server(env):
    client, _, _ = env
    job = wait_job(client, client.post("/api/jobs", json={"prompt": "kleine Eule", "size_mm": 40}).json()["id"])
    assert job["status"] == "ready", job["error"]
    assert job["plan"]["mode"] == "organic"
    assert [s["name"] for s in job["stages"]] == ["refine", "image", "mesh3d", "prepare", "settings", "slice"]
    assert "image.png" in job["files"]
    assert client.get(f"/api/jobs/{job['id']}/files/image.png").headers["content-type"] == "image/png"
    assert job["mesh_stats"]["size_z"] <= 40.01


def test_upload_job_and_delete(env):
    client, _, _ = env
    buf = io.BytesIO()
    trimesh.creation.box((30, 20, 10)).export(buf, file_type="stl")
    r = client.post("/api/jobs/upload", files={"file": ("block.stl", buf.getvalue(), "model/stl")})
    assert r.status_code == 200
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "ready"
    assert job["mesh_stats"]["size_x"] == pytest.approx(30)  # uploads keep their size
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    bad = client.post("/api/jobs/upload", files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400


def test_failed_job_reports_error(env):
    client, _, _ = env
    # parametric without template and without LLM cannot work
    job = wait_job(client, client.post("/api/jobs", json={"prompt": "Zahnrad", "mode": "parametric"}).json()["id"])
    assert job["status"] == "failed" and job["error"]


def test_afs_and_printer_endpoints(env):
    client, _, moon = env
    slots = client.get("/api/afs").json()
    assert len(slots) == 4
    r = client.put("/api/afs/2", json={"material": "petg", "color": "#ff0000", "name": "Rot"})
    assert r.status_code == 200 and r.json()["material"] == "PETG"
    assert client.put("/api/afs/2", json={"material": "unobtainium"}).status_code == 400
    assert client.post("/api/afs/2/select").status_code == 200
    assert client.post("/api/printer", json={"action": "preheat", "script": "PETG"}).status_code == 200
    assert client.post("/api/printer", json={"action": "set_bed", "value": 500}).status_code == 200
    scripts = [json.loads(c)["script"] for m, p, c in moon.requests if p == "/printer/gcode/script"]
    assert "AFS_CHANGE SLOT=2" in scripts and "M140 S75\nM104 S240" in scripts and "M140 S110" in scripts
    assert client.post("/api/printer", json={"action": "rm -rf"}).status_code == 400
    system = client.get("/api/system").json()
    assert system["inference"]["reachable"] is True and system["inference"]["info"]["mock"] is True
    assert system["llm"]["enabled"] is False


def test_static_ui_served(env):
    client, _, _ = env
    html = client.get("/").text
    assert "ARTIFEX" in html and "app.js" in html
    assert client.get("/app.js").status_code == 200


@needs_openscad
def test_full_ai_flow_with_feedback_iteration(cfg, fake_llm):
    fake_llm.handlers["design planner"] = lambda msgs: json.dumps({
        "object_name": "Abstandshalter", "mode": "parametric", "reasoning": "technisches Teil",
        "refined_prompt": "cylindrical spacer", "size_mm": 20, "functional": True, "material": "PETG",
        "scad_spec": "cylinder d=20 h=10 with 5 mm hole", "image_prompt": None,
    })

    def codegen(msgs):
        modify = any("Change it according to this feedback" in m["content"] for m in msgs)
        h = 20 if modify else 10
        return f"```openscad\n$fn=48;\ndifference() {{ cylinder(d=20, h={h}); translate([0,0,-1]) cylinder(d=5, h={h + 2}); }}\n```"

    fake_llm.handlers["OpenSCAD engineer"] = codegen
    fake_llm.handlers["slicing expert"] = lambda msgs: json.dumps({
        "layer_height": 0.16, "perimeters": 5, "infill_density": "40%", "infill_pattern": "triangles",
        "supports": False, "brim_width": 0, "speed_factor": 0.9, "reasoning": "Funktionsteil: stabil drucken.",
    })
    llm = fake_llm.client(cfg)
    svc = build_services(cfg, llm=llm, moonraker=Moonraker(cfg.printer, http=httpx.Client(transport=FakeMoonraker().transport())))
    with TestClient(create_app(cfg, svc, poll_printer=False)) as client:
        job = wait_job(client, client.post("/api/jobs", json={"prompt": "Abstandshalter 20 mm"}).json()["id"])
        assert job["status"] == "ready", job["error"]
        assert job["plan"]["source"] == "llm" and job["settings_source"] == "ai"
        s = job["settings"]
        assert (s["layer_height"], s["perimeters"], s["infill_density"], s["material"]) == (0.16, 5, 0.4, "PLA")
        assert job["mesh_stats"]["size_z"] == pytest.approx(10)

        child = client.post("/api/jobs", json={"prompt": "", "parent_id": job["id"], "feedback": "doppelt so hoch"}).json()
        child = wait_job(client, child["id"])
        assert child["status"] == "ready", child["error"]
        assert child["prompt"] == "Abstandshalter 20 mm"
        assert child["mesh_stats"]["size_z"] == pytest.approx(20)
        # previous code + feedback were sent to the code model
        codegen_calls = [c for c in fake_llm.calls if "OpenSCAD engineer" in c["messages"][0]["content"]]
        assert "doppelt so hoch" in codegen_calls[-1]["messages"][2]["content"]


def test_websocket_streams_job_events(env):
    client, _, _ = env
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "printer"
        job_id = client.post("/api/jobs", json={"prompt": "Wandhaken"}).json()["id"]
        seen = set()
        for _ in range(200):
            ev = ws.receive_json()
            if ev["type"] == "job" and ev["job"]["id"] == job_id:
                seen.add(ev["job"]["status"])
                if ev["job"]["status"] in ("ready", "failed"):
                    break
        assert "running" in seen and ("ready" in seen or "failed" in seen)


def test_interrupted_print_is_detected(env):
    client, svc, moon = env
    from artifex.jobs.models import Job

    job = Job(prompt="x", status="printing", remote_file="artifex/x.gcode")
    svc.jobs.store.save(job)
    # within the grace period nothing happens
    svc.jobs.on_printer_status({"klippy": "ready", "state": "standby", "filename": ""})
    assert svc.jobs.store.get(job.id).status == "printing"
    job.updated -= 60
    svc.jobs.store._db.execute("UPDATE jobs SET data=? WHERE id=?", (job.model_dump_json(), job.id))
    svc.jobs.on_printer_status({"klippy": "ready", "state": "standby", "filename": ""})
    assert svc.jobs.store.get(job.id).status == "failed"
