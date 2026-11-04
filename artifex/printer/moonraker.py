"""Small Moonraker HTTP client (Klipper API server)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import PrinterConfig

log = logging.getLogger(__name__)

STATUS_OBJECTS: dict[str, list[str] | None] = {
    "webhooks": ["state", "state_message"],
    "print_stats": ["state", "filename", "print_duration", "total_duration", "filament_used", "message", "info"],
    "virtual_sdcard": ["progress", "is_active", "file_position"],
    "display_status": ["progress", "message"],
    "extruder": ["temperature", "target", "power"],
    "heater_bed": ["temperature", "target", "power"],
    "toolhead": ["homed_axes", "position", "max_velocity"],
    "fan": ["speed"],
    "fan_generic filter_fan": ["speed"],
    "gcode_macro _AFS": ["current", "pending", "hardware", "slots"],
    "gcode_macro _TOOLHEAD": ["current", "pending", "tools"],
    "pause_resume": ["is_paused"],
    "idle_timeout": ["state"],
}


class MoonrakerError(RuntimeError):
    pass


class Moonraker:
    def __init__(self, cfg: PrinterConfig, http: httpx.Client | None = None):
        self.cfg = cfg
        self.base = cfg.moonraker_url.rstrip("/")
        self._http = http or httpx.Client(timeout=15)
        self._headers = {"X-Api-Key": cfg.moonraker_api_key} if cfg.moonraker_api_key else {}

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, self.base + path, headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise MoonrakerError(f"Moonraker nicht erreichbar: {exc}") from exc
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise MoonrakerError(f"Moonraker {r.status_code}: {msg}")
        return r.json().get("result") if r.content else None

    # -- info / status -----------------------------------------------------------
    def server_info(self) -> dict:
        return self._req("GET", "/server/info")

    def printer_info(self) -> dict:
        return self._req("GET", "/printer/info")

    def query(self, objects: dict[str, list[str] | None] | None = None) -> dict:
        objs = objects or STATUS_OBJECTS
        res = self._req("POST", "/printer/objects/query", json={"objects": objs})
        return res.get("status", {}) if res else {}

    def status(self) -> dict:
        """Compact status for the UI; never raises."""
        out: dict[str, Any] = {"connected": False, "klippy": "offline"}
        try:
            info = self.server_info()
        except MoonrakerError as exc:
            out["error"] = str(exc)
            return out
        out["connected"] = True
        out["klippy"] = info.get("klippy_state", "unknown")
        if out["klippy"] != "ready":
            try:
                out["message"] = self.printer_info().get("state_message", "")
            except MoonrakerError:
                pass
            return out
        try:
            st = self.query()
        except MoonrakerError as exc:
            out["error"] = str(exc)
            return out
        ps = st.get("print_stats", {})
        info_ = ps.get("info") or {}
        afs = st.get("gcode_macro _AFS", {})
        tool = st.get("gcode_macro _TOOLHEAD", {})
        out.update(
            state=ps.get("state", "standby"),
            filename=ps.get("filename", ""),
            print_duration=ps.get("print_duration", 0.0),
            progress=st.get("virtual_sdcard", {}).get("progress", 0.0),
            message=st.get("display_status", {}).get("message") or ps.get("message") or "",
            layer=info_.get("current_layer"),
            total_layers=info_.get("total_layer"),
            extruder=_heater(st.get("extruder")),
            bed=_heater(st.get("heater_bed")),
            homed=st.get("toolhead", {}).get("homed_axes", ""),
            position=st.get("toolhead", {}).get("position"),
            fan=st.get("fan", {}).get("speed", 0.0),
            filter=st.get("fan_generic filter_fan", {}).get("speed"),
            paused=st.get("pause_resume", {}).get("is_paused", False),
            afs_current=afs.get("current"),
            afs_pending=afs.get("pending"),
            afs_hardware=afs.get("hardware"),
            tool=tool.get("current"),
            tool_pending=tool.get("pending"),
        )
        return out

    # -- actions --------------------------------------------------------------------
    def gcode(self, script: str) -> None:
        self._req("POST", "/printer/gcode/script", json={"script": script}, timeout=None)

    def upload(self, path: Path, remote_name: str, start: bool = False) -> str:
        with open(path, "rb") as fh:
            res = self._req(
                "POST",
                "/server/files/upload",
                files={"file": (remote_name, fh, "application/octet-stream")},
                data={"root": "gcodes", "path": self.cfg.upload_folder, "print": "true" if start else "false"},
                timeout=120,
            )
        return (res or {}).get("item", {}).get("path", f"{self.cfg.upload_folder}/{remote_name}")

    def start_print(self, filename: str) -> None:
        self._req("POST", "/printer/print/start", json={"filename": filename})

    def pause(self) -> None:
        self._req("POST", "/printer/print/pause")

    def resume(self) -> None:
        self._req("POST", "/printer/print/resume")

    def cancel(self) -> None:
        self._req("POST", "/printer/print/cancel")

    def emergency_stop(self) -> None:
        self._req("POST", "/printer/emergency_stop")

    def firmware_restart(self) -> None:
        self._req("POST", "/printer/firmware_restart")

    def machine(self, action: str) -> None:
        if action not in ("shutdown", "reboot"):
            raise ValueError(action)
        self._req("POST", f"/machine/{action}")


def _heater(d: dict | None) -> dict | None:
    if not d:
        return None
    return {
        "temp": round(d.get("temperature", 0.0), 1),
        "target": round(d.get("target", 0.0), 1),
        "power": round(d.get("power", 0.0), 2),
    }
