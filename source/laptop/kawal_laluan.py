"""Path safety for kawal: read-only roots, file/find/grep, and the /sync tar unpacker.

Stdlib only (runs on the Python bundled with Unreal). Every path that reaches the disk goes
through `inside()` after `os.path.realpath`, so `..`, absolute paths, drive letters, alternate
data streams and symlinks pointing out of a root are all refused.
"""
from __future__ import annotations

import fnmatch
import io
import os
import re
import tarfile

FILE_MAX_BYTES = 16 * 1024 * 1024
FIND_MAX_RESULTS = 500
GREP_MAX_RESULTS = 200
GREP_FILE_MAX_BYTES = 2 * 1024 * 1024
WALK_MAX_FILES = 300_000
SYNC_MAX_FILES = 20_000
SYNC_MAX_BYTES = 512 * 1024 * 1024

# Roots /sync may write. Content/Gen, Saved, Intermediate, Binaries are never touched.
SYNC_DIR_ROOTS = (("Source",), ("Config",), ("Content", "Data"), ("RawContent",), ("Tools",))


class PathRefused(ValueError):
    """A path or archive member that kawal refuses to touch."""


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def inside(root: str, path: str) -> bool:
    """True when `path` (after resolving symlinks) is `root` or below it."""
    r, p = _norm(root), _norm(path)
    try:
        return os.path.commonpath([r, p]) == r
    except ValueError:  # different drives on Windows
        return False


def split_relative(rel: str) -> list[str]:
    """Split a client-supplied relative path; refuse anything that could escape."""
    if not isinstance(rel, str) or len(rel) > 400:
        raise PathRefused("path missing or too long")
    if "\x00" in rel or ":" in rel:
        raise PathRefused("drive letters and streams are not allowed")
    if rel.startswith(("/", "\\")):
        raise PathRefused("absolute paths are not allowed")
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathRefused("'..' is not allowed")
    return parts


def resolve_under(root: str, rel: str) -> str:
    """Join a relative path to a root and prove the result stays inside it."""
    full = os.path.join(root, *split_relative(rel))
    if not inside(root, full):
        raise PathRefused("path leaves its root")
    return full


def read_file(root: str, rel: str, tail: int | None = None) -> bytes:
    full = resolve_under(root, rel)
    if not os.path.isfile(full):
        raise FileNotFoundError(rel)
    size = os.path.getsize(full)
    with open(full, "rb") as f:
        if tail is not None:
            n = max(0, min(int(tail), FILE_MAX_BYTES))
            f.seek(max(0, size - n))
            return f.read(n)
        if size > FILE_MAX_BYTES:
            raise PathRefused(f"file is {size} bytes; limit {FILE_MAX_BYTES} (use tail=)")
        return f.read()


def _check_glob(glob: str) -> str:
    if not glob or len(glob) > 80 or re.search(r"[\\/:]", glob):
        raise PathRefused("glob must be a file-name pattern without separators")
    return glob


def _walk(root: str, sub: str):
    start = resolve_under(root, sub) if sub else root
    seen = 0
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames.sort()
        # never follow a directory symlink out of the root
        dirnames[:] = [d for d in dirnames if inside(root, os.path.join(dirpath, d))]
        for name in sorted(filenames):
            seen += 1
            if seen > WALK_MAX_FILES:
                return
            yield dirpath, name


def _rel(root: str, full: str) -> str:
    return os.path.relpath(full, root).replace("\\", "/")


def find(root: str, glob: str, sub: str = "") -> dict:
    _check_glob(glob)
    hits: list[str] = []
    for dirpath, name in _walk(root, sub):
        if fnmatch.fnmatch(name.lower(), glob.lower()):
            hits.append(_rel(root, os.path.join(dirpath, name)))
            if len(hits) >= FIND_MAX_RESULTS:
                return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


def grep(root: str, pattern: str, glob: str = "*.h", sub: str = "") -> dict:
    if not pattern or len(pattern) > 200:
        raise PathRefused("pattern missing or longer than 200 characters")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise PathRefused(f"bad regex: {e}") from None
    _check_glob(glob)
    hits: list[dict] = []
    for dirpath, name in _walk(root, sub):
        if not fnmatch.fnmatch(name.lower(), glob.lower()):
            continue
        full = os.path.join(dirpath, name)
        if not inside(root, full) or os.path.getsize(full) > GREP_FILE_MAX_BYTES:
            continue
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            for no, line in enumerate(f, 1):
                if rx.search(line):
                    hits.append({"file": _rel(root, full), "line": no, "text": line.rstrip()[:300]})
                    if len(hits) >= GREP_MAX_RESULTS:
                        return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


def _member_parts(name: str) -> list[str]:
    if "\\" in name:
        raise PathRefused(f"{name!r}: backslash in archive name")
    if name.startswith("/"):
        raise PathRefused(f"{name!r}: absolute path in archive")
    try:
        parts = split_relative(name)
    except PathRefused as e:
        raise PathRefused(f"{name!r}: {e}") from None
    if not parts:
        raise PathRefused(f"{name!r}: empty name")
    return parts


def sync_allowed(parts: list[str]) -> bool:
    """Is this archive path inside one of the roots /sync may write?"""
    if len(parts) == 1:
        return parts[0].lower().endswith(".uproject")
    return any(tuple(parts[: len(r)]) == r and len(parts) > len(r) for r in SYNC_DIR_ROOTS)


def _plan_sync(data: bytes) -> list[tuple[list[str], tarfile.TarInfo]]:
    plan = []
    total = 0
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        members = tar.getmembers()
    except (tarfile.TarError, EOFError, OSError) as e:
        raise PathRefused(f"not a readable tar archive: {e}") from None
    with tar:
        for m in members:
            parts = _member_parts(m.name)
            if m.isdir():
                if not (sync_allowed(parts) or any(r[: len(parts)] == tuple(parts)
                                                    for r in SYNC_DIR_ROOTS)):
                    raise PathRefused(f"{m.name!r}: directory outside the sync roots")
                continue
            if not m.isreg():
                raise PathRefused(f"{m.name!r}: only regular files are allowed (no links)")
            if not sync_allowed(parts):
                raise PathRefused(f"{m.name!r}: outside the sync roots")
            total += m.size
            plan.append((parts, m))
    if len(plan) > SYNC_MAX_FILES or total > SYNC_MAX_BYTES:
        raise PathRefused("archive too large")
    return plan


def _prune(project: str, keep: set[str]) -> int:
    """Delete files under the sync roots that the archive did not carry."""
    removed = 0
    roots = [os.path.join(project, *r) for r in SYNC_DIR_ROOTS]
    for r in roots:
        if not os.path.isdir(r) or not inside(project, r):
            continue
        for dirpath, _dirs, files in os.walk(r):
            for name in files:
                full = os.path.join(dirpath, name)
                if inside(r, full) and os.path.normcase(full) not in keep:
                    os.remove(full)
                    removed += 1
    for name in os.listdir(project):
        full = os.path.join(project, name)
        if (name.lower().endswith(".uproject") and os.path.isfile(full)
                and os.path.normcase(full) not in keep):
            os.remove(full)
            removed += 1
    return removed


def sync(project: str, data: bytes, replace: bool = False) -> dict:
    """Validate the whole archive first, then write. Nothing is written if any member is bad."""
    plan = _plan_sync(data)
    os.makedirs(project, exist_ok=True)
    targets = [(os.path.join(project, *parts), m) for parts, m in plan]
    for full, m in targets:
        # realpath resolves any existing symlinked ancestor before anything is created
        if not inside(project, full):
            raise PathRefused(f"{m.name!r}: resolves outside the project")
    written: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for full, m in targets:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            src = tar.extractfile(m)
            tmp = full + ".kawal-tmp"
            with open(tmp, "wb") as out:
                out.write(src.read() if src else b"")
            os.replace(tmp, full)
            written.add(os.path.normcase(full))
    removed = _prune(project, written) if replace else 0
    return {"written": len(written), "removed": removed}
