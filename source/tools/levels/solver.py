"""Solver (PRD 4.5): breadth-first search over (tick, take, cuts) using the same legal-cut rule as the game.

`solve` finds a winning film with the fewest cuts (par). `count_wins` counts every distinct winning film within the
budget (check c). `cut_table` lists every (tick, from, to) pair with its verdict, which the runtime parity test
replays (solver-legal cuts must pass, the rest must be refused)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from model import Scene
from rules import is_legal_cut, simulate_film


@dataclass(frozen=True)
class Film:
    opening: int
    cuts: tuple[tuple[int, int], ...]

    def as_dict(self) -> dict:
        return {"opening": self.opening, "cuts": [list(c) for c in self.cuts]}


def _status(scene: Scene, t: int, k: int) -> str | None:
    """'fail' / 'win' when the film showing take k ends on tick t, else None."""
    take = scene.takes[k]
    if take.ruined_at(t):
        return "fail"
    if scene.at_goal(take.states[t]):
        return "win"
    return None


def _next_takes(scene: Scene, t: int, k: int, used: int) -> list[tuple[int, int]]:
    """(take, cuts used) reachable on tick t+1 from showing take k on tick t."""
    out = [(k, used)]
    for b in range(len(scene.takes)):
        if b != k and is_legal_cut(scene, t + 1, k, b, used).ok:
            out.append((b, used + 1))
    return out


def solve(scene: Scene) -> Film | None:
    """Fewest-cut winning film; ties go to the earliest win, then the lowest take numbers (deterministic)."""
    frontier: dict[tuple[int, int], Film] = {}
    for k in range(len(scene.takes)):
        frontier[(k, 0)] = Film(k, ())
    best: tuple[int, int, Film] | None = None
    for t in range(scene.ticks):
        nxt: dict[tuple[int, int], Film] = {}
        alive = []
        for (k, used), film in sorted(frontier.items()):
            st = _status(scene, t, k)
            if st == "win":
                if best is None or used < best[0]:
                    best = (used, t, film)
            elif st is None and t + 1 < scene.ticks:
                alive.append((k, used, film))
        for k, used, film in alive:  # staying first: an earlier cut wins over a later one into the same state
            nxt.setdefault((k, used), film)
        for k, used, film in alive:
            for b, u in _next_takes(scene, t, k, used)[1:]:
                nxt.setdefault((b, u), Film(film.opening, film.cuts + ((t + 1, b),)))
        frontier = nxt
        if best is not None and best[0] == 0:
            break
    return best[2] if best else None


def count_wins(scene: Scene, limit: int | None = None) -> int:
    """Number of distinct winning edit lists within the budget (cuts after the win do not make a new film; a cut
    between two takes that are still identical counts as a different edit)."""

    @lru_cache(maxsize=None)
    def ways(t: int, k: int, used: int) -> int:
        st = _status(scene, t, k)
        if st == "win":
            return 1
        if st == "fail" or t + 1 >= scene.ticks:
            return 0
        return sum(ways(t + 1, b, u) for b, u in _next_takes(scene, t, k, used))

    total = sum(ways(0, k, 0) for k in range(len(scene.takes)))
    ways.cache_clear()
    return min(total, limit) if limit is not None else total


def winning_films(scene: Scene, limit: int = 50) -> list[Film]:
    """Enumerate winning films (depth-first, deterministic order), at most `limit`."""
    found: list[Film] = []

    def walk(t: int, k: int, used: int, film: Film) -> None:
        if len(found) >= limit:
            return
        st = _status(scene, t, k)
        if st == "win":
            found.append(film)
            return
        if st == "fail" or t + 1 >= scene.ticks:
            return
        for b, u in _next_takes(scene, t, k, used):
            walk(t + 1, b, u, film if b == k else Film(film.opening, film.cuts + ((t + 1, b),)))

    for k in range(len(scene.takes)):
        walk(0, k, 0, Film(k, ()))
    return found


def solo_winners(scene: Scene) -> list[int]:
    """Takes that win with no cut at all (check a)."""
    return [k for k in range(len(scene.takes)) if simulate_film(scene, k, []).won]


def cut_table(scene: Scene) -> list[tuple[int, int, int, bool, str | None]]:
    """(tick, from, to, legal, reason) for every tick 1..ticks-1 and ordered take pair; the budget is ignored."""
    open_budget = replace(scene, budget=max(1, scene.budget))
    n = len(scene.takes)
    rows = []
    for t in range(1, scene.ticks):
        for a in range(n):
            for b in range(n):
                if a != b:
                    chk = is_legal_cut(open_budget, t, a, b)
                    rows.append((t, a, b, chk.ok, chk.reason))
    return rows


def verify_solution(scene: Scene) -> list[str]:
    """Check h: the stored solution wins within the budget and uses exactly par cuts (when par is stored)."""
    if not scene.solution:
        return ["no stored solution"]
    res = simulate_film(scene, scene.solution["opening"], scene.solution["cuts"])
    errs = []
    if not res.won:
        errs.append(f"stored solution does not win: {res.outcome} on tick {res.tick} ({res.reason})")
    elif res.ignored_cuts:
        errs.append(f"stored solution has {res.ignored_cuts} cut(s) after the win")
    if scene.par is not None and len(scene.solution["cuts"]) != scene.par:
        errs.append(f"stored solution uses {len(scene.solution['cuts'])} cuts, par is {scene.par}")
    return errs


def dead_end_cuts(scene: Scene) -> list[tuple[int, int, int]]:
    """Check d: legal cuts a player can reach that turn a film which could still win into one that cannot.

    Returns (tick, from, to) for each such decoy cut, deduplicated over cut counts."""

    @lru_cache(maxsize=None)
    def can_win(t: int, k: int, used: int) -> bool:
        st = _status(scene, t, k)
        if st == "win":
            return True
        if st == "fail" or t + 1 >= scene.ticks:
            return False
        return any(can_win(t + 1, b, u) for b, u in _next_takes(scene, t, k, used))

    seen: set[tuple[int, int, int]] = set()
    out: set[tuple[int, int, int]] = set()
    stack = [(0, k, 0) for k in range(len(scene.takes))]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        t, k, used = node
        if _status(scene, t, k) is not None or t + 1 >= scene.ticks:
            continue
        alive = can_win(t, k, used)
        for b, u in _next_takes(scene, t, k, used):
            if b != k and alive and not can_win(t + 1, b, u):
                out.add((t + 1, k, b))
            stack.append((t + 1, b, u))
    can_win.cache_clear()
    return sorted(out)


def earliest_win(scene: Scene) -> int | None:
    """Earliest tick any film within the budget wins on (any cut count). The generator sets the scene length from
    it, so the clock is part of the puzzle: a take that is too slow cannot finish the film."""
    frontier = {(k, 0) for k in range(len(scene.takes))}
    for t in range(scene.ticks):
        status = {(k, used): _status(scene, t, k) for k, used in frontier}
        if "win" in status.values():
            return t
        if t + 1 >= scene.ticks:
            break
        frontier = {nb for (k, used), st in status.items() if st is None for nb in _next_takes(scene, t, k, used)}
    return None
