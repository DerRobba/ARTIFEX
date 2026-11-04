"""ARTIFEX command line.

    artifex serve                      # web server + touch UI (systemd service)
    artifex generate "kleine Eule"     # run the AI pipeline once in the terminal
    artifex slice model.stl -o x.gcode # slice an STL with the in-house slicer
    artifex config                     # print the effective configuration
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="artifex", description="ARTIFEX printer host software")
    ap.add_argument("-c", "--config", help="path to artifex.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)

    g = sub.add_parser("generate", help="prompt -> G-code without the web UI")
    g.add_argument("prompt")
    g.add_argument("--mode", default="auto", choices=["auto", "organic", "parametric"])
    g.add_argument("--size", type=float)
    g.add_argument("--slot", type=int, default=0)

    sl = sub.add_parser("slice", help="slice an STL")
    sl.add_argument("stl")
    sl.add_argument("-o", "--output", default=None)
    sl.add_argument("--material", default="PLA")
    sl.add_argument("--layer-height", type=float)
    sl.add_argument("--infill", type=float, help="0..1")
    sl.add_argument("--supports", action="store_true")
    sl.add_argument("--no-center", action="store_true", help="do not re-center on the bed")

    sub.add_parser("config", help="show effective configuration")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .config import load_config

    cfg = load_config(args.config)
    cmd = args.cmd or "serve"

    if cmd == "config":
        print(json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False))
        return 0

    if cmd == "serve":
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(cfg), host=args.host or cfg.server.host, port=args.port or cfg.server.port,
                    log_level="info")
        return 0

    if cmd == "slice":
        from .pipeline.mesh import prepare_mesh
        from .slicer import ArtifexSlicer, SliceSettings

        src = Path(args.stl)
        out = Path(args.output or src.with_suffix(".gcode"))
        prepared = out.with_suffix(".prepared.stl")
        if args.no_center:
            prepared = src
        else:
            prepare_mesh(src, prepared, cfg, size_mm=None, organic=False)
        s = SliceSettings.for_material(args.material, bed_x=cfg.printer.bed_x, bed_y=cfg.printer.bed_y)
        over = {"layer_height": args.layer_height, "infill_density": args.infill}
        if args.supports:
            over["supports"] = True
        s = s.with_overrides(over)
        r = ArtifexSlicer().slice(prepared, out, s, lambda m, f: print(f"[{f * 100:5.1f}%] {m}", file=sys.stderr))
        print(json.dumps(r.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if cmd == "generate":
        from .pipeline.plan import JobOptions
        from .server import build_services

        svc = build_services(cfg)
        job = svc.jobs.submit_prompt(args.prompt, JobOptions(mode=args.mode, size_mm=args.size, slot=args.slot,
                                                             auto_print=False))
        # run synchronously instead of in the worker thread
        svc.jobs._queue.get_nowait()
        svc.jobs.publish = lambda ev: (ev.get("type") == "job" and print(
            f"[{ev['job']['progress'] * 100:5.1f}%] {ev['job']['message']}", file=sys.stderr))
        svc.jobs.run(job)
        job = svc.jobs.store.get(job.id)
        print(json.dumps({"id": job.id, "status": job.status, "error": job.error, "plan": job.plan and job.plan.model_dump(),
                          "slice": job.slice, "dir": str(svc.jobs.jobs_dir / job.id)}, indent=2, ensure_ascii=False))
        return 0 if job.status == "ready" else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
