"""kawal: the laptop side of the One Good Take link (PRD 4.2).

A tiny HTTP service on 127.0.0.1 that the host reaches only through the reverse SSH tunnel this
process starts. Every request carries the kit token. The endpoint list is closed: there is no
"run any command" route, every command line is built from validated fields, and the service
writes only below C:\\MegaJam. Stdlib only, so it runs on the Python bundled with Unreal:

    <UE>\\Engine\\Binaries\\ThirdParty\\Python3\\Win64\\python.exe kawal.py --config config.json
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python ignores cwd

import kawal_job as kj  # noqa: E402
import kawal_laluan as kl  # noqa: E402

KIT_VERSION = "1"
TOKEN_HEADER = "X-Kawal-Token"
SYNC_MAX_BODY = 256 * 1024 * 1024
JSON_MAX_BODY = 64 * 1024
IDLE_FOR_WINDOW_S = 600
LAUNCHER_DAT = r"C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat"


class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


def find_ue(dat_path: str, app_prefix: str = "UE_5.8") -> str | None:
    """Unreal 5.8 install folder from the Epic launcher manifest."""
    try:
        with open(dat_path, encoding="utf-8") as f:
            items = json.load(f).get("InstallationList", [])
    except (OSError, ValueError):
        return None
    for it in items:
        if str(it.get("AppName", "")).startswith(app_prefix) and it.get("InstallLocation"):
            return it["InstallLocation"]
    return None


def windows_idle_s() -> float | None:
    """Seconds since the owner last touched keyboard or mouse (None off Windows)."""
    if os.name != "nt":
        return None
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0


def _quiet(argv: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                           creationflags=kj.NO_WINDOW)
        return r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"error: {e}"


class Kawal:
    """State and routes; HTTP-free so the tests can drive it directly or over a socket."""

    def __init__(self, cfg: dict, spawn=kj.default_spawn, idle=windows_idle_s, clock=time.time):
        self.cfg = cfg
        self.token = str(cfg["token"])
        if len(self.token) < 32:
            raise ValueError("token must be at least 32 characters")
        ue = cfg.get("ue_root") or find_ue(cfg.get("launcher_dat", LAUNCHER_DAT)) or ""
        self.p = kj.Paths(cfg["megajam"], ue)
        self.spawn, self.idle, self.clock = spawn, idle, clock
        self.jobs = kj.Jobs(self.p.jobs, spawn=spawn, clock=clock)
        self.errors: list[str] = []
        self.editor_proc = None
        self.tunnel: kj.Tunnel | None = None
        self.captures = cfg.get("captures_dir") or os.path.join(
            os.path.expanduser("~"), "Videos", "Captures")
        self.installed_at = float(cfg.get("installed_at", 0))

    # ---- helpers ---------------------------------------------------------------------------
    def authorised(self, given: str | None) -> bool:
        return given is not None and hmac.compare_digest(given.encode(), self.token.encode())

    def note_error(self, msg: str) -> None:
        self.errors = (self.errors + [f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"])[-20:]

    def paused(self) -> bool:
        return os.path.exists(self.p.pause_flag)

    def read_roots(self) -> dict[str, str]:
        return {"engine": self.p.ue, "project": self.p.project, "out": self.p.out}

    def _root(self, q: dict) -> str:
        root = self.read_roots().get(q.get("root", ""))
        if not root:
            raise HttpError(400, "root must be engine, project or out")
        if not os.path.isdir(root):
            raise HttpError(404, "root folder does not exist yet")
        return root

    def _may_open_window(self, body: dict) -> None:
        idle = self.idle()
        if body.get("paksa") in (1, True, "1"):
            return
        if idle is None or idle < IDLE_FOR_WINDOW_S:
            raise HttpError(423, f"owner active (idle_s={idle}); windowed jobs need "
                                 f"idle_s >= {IDLE_FOR_WINDOW_S} or paksa=1")

    def _job_guard(self) -> None:
        if self.paused():
            raise HttpError(423, "paused by owner (JEDA)")
        if not self.p.ue or not os.path.isdir(self.p.ue):
            raise HttpError(503, "Unreal 5.8 not found")

    # ---- GET -------------------------------------------------------------------------------
    def status(self) -> dict:
        disk = shutil.disk_usage(self.p.megajam) if os.path.isdir(self.p.megajam) else None
        ue_version = None
        try:
            with open(os.path.join(self.p.ue, "Engine", "Build", "Build.version")) as f:
                v = json.load(f)
            ue_version = f"{v['MajorVersion']}.{v['MinorVersion']}.{v['PatchVersion']}"
        except (OSError, ValueError, KeyError):
            pass
        editor_up = self.editor_proc is not None and self.editor_proc.poll() is None
        return {"ok": True, "kit": KIT_VERSION, "ue_root": self.p.ue, "ue_version": ue_version,
                "build_tools": {"build_bat": os.path.isfile(self.p.build_bat),
                                "uat": os.path.isfile(self.p.uat),
                                "editor_cmd": os.path.isfile(self.p.editor_cmd)},
                "disk_free_gb": round(disk.free / 2**30, 1) if disk else None,
                "idle_s": self.idle(), "paused": self.paused(), "editor": editor_up,
                "job": self.jobs.running(),
                "tunnel": self.tunnel.state if self.tunnel else "off"}

    def doktor(self) -> dict:
        rep = {"status": self.status(), "platform": platform.platform(),
               "python": sys.version, "errors": self.errors,
               "paths": {k: os.path.exists(v) for k, v in vars(self.p).items()}}
        if os.name == "nt":
            vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                                   "Microsoft Visual Studio", "Installer", "vswhere.exe")
            rep["vswhere"] = _quiet([vswhere, "-all", "-prerelease", "-products", "*",
                                     "-format", "json", "-include", "packages"], 60)[:200_000]
            rep["gpu"] = _quiet(["powershell", "-NoProfile", "-Command",
                                 "Get-CimInstance Win32_VideoController | "
                                 "Select-Object Name,DriverVersion | ConvertTo-Json"])
            rep["windows"] = _quiet(["cmd", "/c", "ver"])
            kits = r"C:\Program Files (x86)\Windows Kits\10\Include"
            rep["windows_sdks"] = sorted(os.listdir(kits)) if os.path.isdir(kits) else []
        sdk_json = os.path.join(self.p.ue, "Engine", "Config", "Windows", "Windows_SDK.json")
        if os.path.isfile(sdk_json):
            with open(sdk_json, encoding="utf-8", errors="replace") as f:
                rep["ue_windows_sdk_json"] = f.read(8000)
        return rep

    def list_captures(self) -> list[dict]:
        if not os.path.isdir(self.captures):
            return []
        out = []
        for name in sorted(os.listdir(self.captures)):
            full = os.path.join(self.captures, name)
            if os.path.isfile(full) and os.path.getmtime(full) >= self.installed_at:
                out.append({"name": name, "bytes": os.path.getsize(full),
                            "mtime": os.path.getmtime(full)})
        return out

    def capture_path(self, name: str) -> str:
        if name not in {c["name"] for c in self.list_captures()}:
            raise HttpError(404, "no such capture (only files made after the kit was installed)")
        return os.path.join(self.captures, name)

    def handle_get(self, path: str, q: dict):
        """Returns a dict (JSON) or ('file', abs_path) or ('bytes', data)."""
        if path == "/status":
            return self.status()
        if path == "/doktor":
            return self.doktor()
        if path.startswith("/job/"):
            job = self.jobs.get(path[len("/job/"):])
            if job is None:
                raise HttpError(404, "no such job")
            return job
        if path == "/file":
            tail = int(q["tail"]) if q.get("tail", "").isdigit() else None
            return ("bytes", kl.read_file(self._root(q), q.get("path", ""), tail))
        if path == "/find":
            return kl.find(self._root(q), q.get("glob", ""), q.get("sub", ""))
        if path == "/grep":
            return kl.grep(self._root(q), q.get("q", ""), q.get("glob", "*.h"), q.get("sub", ""))
        if path == "/captures":
            if q.get("name"):
                return ("file", self.capture_path(q["name"]))
            return {"captures": self.list_captures()}
        raise HttpError(404, "unknown endpoint")

    # ---- POST ------------------------------------------------------------------------------
    def handle_post(self, path: str, q: dict, body: bytes) -> dict:
        if path == "/sync":
            if self.jobs.running():
                raise HttpError(409, "a job is running; sync after it ends")
            return kl.sync(self.p.project, body, replace=q.get("ganti") == "1")
        data = self._json(body)
        if path == "/job/stop":
            return {"stopped": self.jobs.stop()}
        if path == "/editor":
            return self._editor(data)
        if path not in ("/job/build", "/job/content", "/job/test", "/job/run",
                        "/job/package", "/job/zip", "/publish"):
            raise HttpError(404, "unknown endpoint")
        self._job_guard()
        return self._start(path, data)

    def _json(self, body: bytes) -> dict:
        if not body:
            return {}
        try:
            data = json.loads(body)
        except ValueError:
            raise HttpError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        return data

    def _start(self, path: str, d: dict) -> dict:
        p, cwd = self.p, self.p.project
        if path == "/job/build":
            argv = kj.build_argv(p, d.get("target", "Editor"))
            return self.jobs.start_proc("build", argv, cwd, kj.TIMEOUT_S["build"])
        if path == "/job/content":
            return self.jobs.start_proc("content", kj.content_argv(p), cwd, kj.TIMEOUT_S["content"])
        if path == "/job/test":
            argv = kj.test_argv(p, d.get("filter", "OneGoodTake"), os.path.join(p.out, "tests"))
            return self.jobs.start_proc("test", argv, cwd, kj.TIMEOUT_S["test"])
        if path == "/job/run":
            cmds = kj.check_exec_cmds(d.get("cmds"))
            timeout = d.get("had_s", 300)
            if not isinstance(timeout, int) or not 10 <= timeout <= kj.RUN_TIMEOUT_MAX_S:
                raise HttpError(400, f"had_s must be 10 to {kj.RUN_TIMEOUT_MAX_S}")
            self._may_open_window(d)
            ue_log = os.path.join(p.jobs, f"run-{int(self.clock())}-ue.log")
            argv = kj.run_argv(p, cmds, d.get("where", "editor"), ue_log)
            return self.jobs.start_proc("run", argv, cwd, timeout, windowed=True)
        if path == "/job/package":
            argv = kj.package_argv(p, d.get("platform", "Win64"))
            return self.jobs.start_proc("package", argv, cwd, kj.TIMEOUT_S["package"])
        if path == "/job/zip":
            team = d.get("team", "")
            if not kj.TEAM_RX.match(team if isinstance(team, str) else ""):
                raise HttpError(400, "team must be 1 to 32 letters or digits")
            return self.jobs.start_fn("zip", lambda log: kj.zip_build(p, team, log))
        return self.jobs.start_fn("publish", lambda log: kj.publish_build(p, log))

    def _editor(self, d: dict) -> dict:
        action = d.get("aksi")
        if action == "henti":
            if self.editor_proc is not None and self.editor_proc.poll() is None:
                kj.kill_tree(self.editor_proc)
            self.editor_proc = None
            return {"editor": False}
        if action != "mula":
            raise HttpError(400, "aksi must be mula or henti")
        self._job_guard()
        self._may_open_window(d)
        if self.editor_proc is not None and self.editor_proc.poll() is None:
            return {"editor": True}
        os.makedirs(self.p.jobs, exist_ok=True)
        log = open(os.path.join(self.p.jobs, "editor.log"), "ab")
        self.editor_proc = self.spawn([self.p.editor, self.p.uproject], self.p.project, log, True)
        return {"editor": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "kawal"
    sys_version = ""
    kawal: Kawal  # set by make_server

    def log_message(self, fmt, *args):  # quiet; errors go to /doktor
        pass

    def _send(self, code: int, payload, ctype="application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, full: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(full)))
        self.end_headers()
        with open(full, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def _dispatch(self, method: str) -> None:
        k = self.kawal
        if not k.authorised(self.headers.get(TOKEN_HEADER)):
            self._send(401, {"error": "token missing or wrong"})
            return
        url = urlsplit(self.path)
        q = {key: v[0] for key, v in parse_qs(url.query).items()}
        try:
            if method == "GET":
                res = k.handle_get(url.path, q)
            else:
                n = int(self.headers.get("Content-Length") or 0)
                limit = SYNC_MAX_BODY if url.path == "/sync" else JSON_MAX_BODY
                if n < 0 or n > limit:
                    raise HttpError(413, f"body over {limit} bytes")
                res = k.handle_post(url.path, q, self.rfile.read(n))
        except HttpError as e:
            self._send(e.code, {"error": e.msg})
            return
        except kj.Busy as e:
            self._send(409, {"error": str(e)})
            return
        except (kl.PathRefused, kj.Refused) as e:
            self._send(400, {"error": str(e)})
            return
        except FileNotFoundError as e:
            self._send(404, {"error": f"not found: {e}"})
            return
        except Exception as e:  # keep serving; report through /doktor
            k.note_error(f"{method} {url.path}: {type(e).__name__}: {e}")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if isinstance(res, tuple) and res[0] == "file":
            self._send_file(res[1])
        elif isinstance(res, tuple):
            self._send(200, res[1], "application/octet-stream")
        else:
            self._send(200, res)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(kawal: Kawal, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"kawal": kawal})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="kawal: fixed-command laptop service")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.json"))
    args = ap.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    port = int(cfg.get("listen_port", 8765))
    k = Kawal(cfg)
    srv = make_server(k, port)
    if cfg.get("tunnel"):
        os.makedirs(k.p.out, exist_ok=True)
        k.tunnel = kj.Tunnel(kj.ssh_argv(cfg["tunnel"], port), os.path.join(k.p.out, "ssh.log"))
        k.tunnel.start()
    pid_file = os.path.join(k.p.out, "kawal.pid")  # kit.py hidup/henti (MULA.bat, BERHENTI.bat)
    os.makedirs(k.p.out, exist_ok=True)
    with open(pid_file, "w", encoding="ascii") as f:
        f.write(str(os.getpid()))
    print(f"kawal {KIT_VERSION} listening on 127.0.0.1:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if k.tunnel:
            k.tunnel.stop()
        srv.server_close()
        try:
            os.remove(pid_file)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    threading.current_thread().name = "kawal"
    sys.exit(main())
