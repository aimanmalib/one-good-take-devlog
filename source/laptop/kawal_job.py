"""Jobs for kawal: one background job at a time, fixed command builders, the SSH tunnel.

Every command line is built here from validated fields; no request text reaches a shell.
`spawn` is injectable so the host tests run without Windows or Unreal.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

LOG_TAIL_BYTES = 16 * 1024
EXEC_PREFIX = "ogt."
EXEC_MAX_CMDS = 20
EXEC_RX = re.compile(r"^ogt\.[A-Za-z][A-Za-z0-9_.]{0,40}( [A-Za-z0-9_.:\-]{1,40}){0,8}$")
TEST_FILTER_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,80}$")
TEAM_RX = re.compile(r"^[A-Za-z0-9]{1,32}$")
BUILD_TARGETS = {"Editor": "OneGoodTakeEditor", "Game": "OneGoodTake"}
PACKAGE_PLATFORMS = ("Win64", "Android")
TIMEOUT_S = {"build": 3 * 3600, "content": 3600, "test": 3600, "package": 4 * 3600}
RUN_TIMEOUT_MAX_S = 1800
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Refused(ValueError):
    """A request field that fails validation (HTTP 400)."""


class Busy(RuntimeError):
    """Another job is still running (HTTP 409)."""


def check_exec_cmds(cmds) -> str:
    """Validate the -ExecCmds list: `ogt.` console commands only, no separators or quotes."""
    if not isinstance(cmds, list) or not 1 <= len(cmds) <= EXEC_MAX_CMDS:
        raise Refused(f"cmds must be a list of 1 to {EXEC_MAX_CMDS} strings")
    for c in cmds:
        if not isinstance(c, str) or not c.startswith(EXEC_PREFIX) or not EXEC_RX.match(c):
            raise Refused(f"command refused (only '{EXEC_PREFIX}' commands, plain arguments)")
    return ",".join(cmds)


def kill_tree(proc) -> None:
    if os.name == "nt" and getattr(proc, "pid", None):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW, check=False)
    else:
        proc.kill()


def default_spawn(argv, cwd, log, windowed=False):
    return subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL,
                            creationflags=0 if windowed else NO_WINDOW)


class Paths:
    """All laptop paths, derived from C:\\MegaJam and the Unreal root."""

    def __init__(self, megajam: str, ue_root: str):
        self.megajam = megajam
        self.ue = ue_root
        self.project = os.path.join(megajam, "OneGoodTake")
        self.uproject = os.path.join(self.project, "OneGoodTake.uproject")
        self.out = os.path.join(megajam, "out")
        self.jobs = os.path.join(self.out, "jobs")
        self.package = os.path.join(self.out, "package")
        self.hantar = os.path.join(megajam, "HANTAR")
        self.latest = os.path.join(megajam, "builds", "latest")
        self.pause_flag = os.path.join(megajam, "JEDA")
        win64 = os.path.join(ue_root, "Engine", "Binaries", "Win64")
        self.editor = os.path.join(win64, "UnrealEditor.exe")
        self.editor_cmd = os.path.join(win64, "UnrealEditor-Cmd.exe")
        bat = os.path.join(ue_root, "Engine", "Build", "BatchFiles")
        self.build_bat = os.path.join(bat, "Build.bat")
        self.uat = os.path.join(bat, "RunUAT.bat")


def build_argv(p: Paths, target: str) -> list[str]:
    if target not in BUILD_TARGETS:
        raise Refused(f"target must be one of {sorted(BUILD_TARGETS)}")
    return [p.build_bat, BUILD_TARGETS[target], "Win64", "Development",
            f"-Project={p.uproject}", "-WaitMutex", "-NoHotReloadFromIDE"]


def content_argv(p: Paths) -> list[str]:
    script = os.path.join(p.project, "Tools", "Editor", "build_content.py")
    return [p.editor_cmd, p.uproject, "-run=pythonscript", f"-script={script}",
            "-unattended", "-nop4", "-nosplash", "-stdout", "-FullStdOutLogOutput"]


def test_argv(p: Paths, filt: str, report_dir: str) -> list[str]:
    if not TEST_FILTER_RX.match(filt or ""):
        raise Refused("filter must be a dotted test name")
    return [p.editor_cmd, p.uproject, f"-ExecCmds=Automation RunTests {filt};Quit",
            "-nullrhi", "-unattended", "-nop4", "-nosplash", "-stdout",
            "-TestExit=Automation Test Queue Empty", f"-ReportExportPath={report_dir}"]


def run_argv(p: Paths, cmds: str, where: str, ue_log: str) -> list[str]:
    tail = ["-windowed", "-ResX=1280", "-ResY=720", f"-ExecCmds={cmds}", f"-abslog={ue_log}"]
    if where == "editor":
        return [p.editor, p.uproject, "-game", "-nosplash"] + tail
    if where == "build":
        return [os.path.join(p.latest, "OneGoodTake.exe")] + tail
    raise Refused("where must be 'editor' or 'build'")


def package_argv(p: Paths, platform: str) -> list[str]:
    if platform not in PACKAGE_PLATFORMS:
        raise Refused(f"platform must be one of {list(PACKAGE_PLATFORMS)}")
    argv = [p.uat, "BuildCookRun", f"-project={p.uproject}", "-noP4", f"-platform={platform}",
            "-clientconfig=Development", "-build", "-cook", "-stage", "-pak", "-archive",
            f"-archivedirectory={p.package}", "-utf8output", "-unattended"]
    if platform == "Android":
        argv.append("-cookflavor=ASTC")
    return argv


def packaged_dir(p: Paths) -> str:
    return os.path.join(p.package, "Windows")


def zip_build(p: Paths, team: str, log) -> None:
    if not TEAM_RX.match(team or ""):
        raise Refused("team must be 1 to 32 letters or digits")
    src = packaged_dir(p)
    if not os.path.isdir(src):
        raise FileNotFoundError("no packaged Win64 build; run /job/package first")
    os.makedirs(p.hantar, exist_ok=True)
    dest = os.path.join(p.hantar, f"{team}_OneGoodTake.zip")
    tmp = dest + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _dirs, files in os.walk(src):
            for name in sorted(files):
                full = os.path.join(dirpath, name)
                z.write(full, os.path.join("OneGoodTake", os.path.relpath(full, src)))
    os.replace(tmp, dest)
    log.write(f"zip {dest} {os.path.getsize(dest)} bytes\n".encode())


def publish_build(p: Paths, log) -> None:
    src = packaged_dir(p)
    if not os.path.isdir(src):
        raise FileNotFoundError("no packaged Win64 build; run /job/package first")
    staging = p.latest + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    shutil.rmtree(p.latest, ignore_errors=True)
    os.replace(staging, p.latest)
    log.write(f"published {p.latest}\n".encode())


class Jobs:
    """At most one job runs. Each job logs to out/jobs/<id>.log."""

    def __init__(self, log_dir: str, spawn=default_spawn, clock=time.time):
        self.log_dir = log_dir
        self.spawn = spawn
        self.clock = clock
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.current: str | None = None
        self.proc = None
        self.seq = 0

    def running(self) -> str | None:
        return self.current

    def _new(self, kind: str) -> dict:
        if self.current is not None:
            raise Busy(f"job {self.current} is still running")
        self.seq += 1
        jid = f"j{int(self.clock())}-{self.seq}"
        os.makedirs(self.log_dir, exist_ok=True)
        job = {"id": jid, "kind": kind, "state": "running", "rc": None,
               "started": self.clock(), "ended": None,
               "log": os.path.join(self.log_dir, jid + ".log")}
        self.jobs[jid] = job
        self.current = jid
        return job

    def _finish(self, job: dict, state: str, rc) -> None:
        with self.lock:
            job.update(state=state, rc=rc, ended=self.clock())
            self.current = None
            self.proc = None

    def start_proc(self, kind, argv, cwd, timeout_s, windowed=False) -> dict:
        with self.lock:
            job = self._new(kind)
            log = open(job["log"], "ab")
            log.write((" ".join(argv) + "\n").encode())
            log.flush()
            try:
                self.proc = self.spawn(argv, cwd, log, windowed)
            except OSError as e:
                log.write(f"spawn failed: {e}\n".encode())
                log.close()
                self.current = None
                job.update(state="failed", rc=None, ended=self.clock())
                return job
        threading.Thread(target=self._watch, args=(job, self.proc, log, timeout_s),
                         daemon=True).start()
        return job

    def _watch(self, job, proc, log, timeout_s) -> None:
        try:
            rc = proc.wait(timeout=timeout_s)
            state = "ok" if rc == 0 else "failed"
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            rc, state = None, "timeout"
        if job.get("stop"):
            state = "stopped"
        log.close()
        self._finish(job, state, rc)

    def start_fn(self, kind, fn) -> dict:
        with self.lock:
            job = self._new(kind)
        threading.Thread(target=self._run_fn, args=(job, fn), daemon=True).start()
        return job

    def _run_fn(self, job, fn) -> None:
        with open(job["log"], "ab") as log:
            try:
                fn(log)
                state = "ok"
            except Exception as e:  # reported through the job log, never raised to HTTP
                log.write(f"{type(e).__name__}: {e}\n".encode())
                state = "failed"
        self._finish(job, state, 0 if state == "ok" else 1)

    def stop(self) -> str | None:
        with self.lock:
            jid, proc = self.current, self.proc
            if jid and proc is not None:
                self.jobs[jid]["stop"] = True
                kill_tree(proc)
        return jid

    def get(self, jid: str) -> dict | None:
        job = self.jobs.get(jid)
        if job is None:
            return None
        out = {k: v for k, v in job.items() if k not in ("log", "stop")}
        try:
            with open(job["log"], "rb") as f:
                f.seek(max(0, os.path.getsize(job["log"]) - LOG_TAIL_BYTES))
                out["tail"] = f.read().decode("utf-8", "replace")
        except OSError:
            out["tail"] = ""
        return out


def ssh_argv(t: dict, local_port: int) -> list[str]:
    """The reverse tunnel, exactly as PRD 4.2: two loopback -R forwards, strict host key."""
    return [t.get("ssh", "ssh"), "-N", "-T", "-i", t["key"], "-o", "IdentitiesOnly=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={t['known_hosts']}", "-o", "BatchMode=yes",
            "-R", f"127.0.0.1:{int(t['remote_kawal'])}:127.0.0.1:{int(local_port)}",
            "-R", f"127.0.0.1:{int(t['remote_mcp'])}:127.0.0.1:{int(t.get('mcp_local', 8000))}",
            f"{t['user']}@{t['host']}"]


class Tunnel(threading.Thread):
    """Keeps ssh alive with exponential backoff (2 s doubling to 5 min; reset after 60 s up)."""

    def __init__(self, argv, log_path, spawn=default_spawn, sleep=time.sleep):
        super().__init__(daemon=True)
        self.argv, self.log_path, self.spawn, self.sleep = argv, log_path, spawn, sleep
        self.state = "starting"
        self.restarts = 0
        self.proc = None
        self.stopped = threading.Event()

    def run(self) -> None:
        delay = 2
        while not self.stopped.is_set():
            began = time.time()
            with open(self.log_path, "ab") as log:
                try:
                    self.proc = self.spawn(self.argv, None, log, False)
                    self.state = "running"
                    rc = self.proc.wait()
                    log.write(f"ssh exited {rc}\n".encode())
                except OSError as e:
                    log.write(f"ssh failed to start: {e}\n".encode())
            self.state = "down"
            self.restarts += 1
            delay = 2 if time.time() - began > 60 else min(300, delay * 2)
            self.sleep(delay)

    def stop(self) -> None:
        self.stopped.set()
        if self.proc is not None:
            kill_tree(self.proc)
