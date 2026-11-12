"""Legal-cut rule and film simulation (PRD 4.4). The game runtime implements the same two functions in C++
(`IsLegalCut`, `SimulateFilm`); P9/P12 tests replay solver output through both."""
from __future__ import annotations

from dataclasses import dataclass, field

from model import Scene

# Reason codes. The script supervisor shows the first three to the player; the rest are editing-desk limits.
POSITION, PROP, AIRBORNE = "position", "prop", "airborne"
RUINED, BUDGET, SAME_TICK, SAME_TAKE, OUT_OF_FILM = "ruined", "budget", "same_tick", "same_take", "out_of_film"


@dataclass(frozen=True)
class CutCheck:
    ok: bool
    reason: str | None = None


def is_legal_cut(scene: Scene, t: int, a: int, b: int, cuts_used: int = 0, last_cut_tick: int | None = None) -> CutCheck:
    """May the film cut from take a to take b on tick t?

    Both Lucky are standing (not in the air) in the same cell with the same carry state; take b is not ruined on t
    (before its fail tick, or after it recovers); the budget is not spent; at most one cut per tick. Tick 0 is the
    opening choice, not a cut."""
    if a == b:
        return CutCheck(False, SAME_TAKE)
    if not (0 <= a < len(scene.takes) and 0 <= b < len(scene.takes)) or not 1 <= t < scene.ticks:
        return CutCheck(False, OUT_OF_FILM)
    if last_cut_tick is not None and t <= last_cut_tick:
        return CutCheck(False, SAME_TICK)
    if cuts_used >= scene.budget:
        return CutCheck(False, BUDGET)
    ta, tb = scene.takes[a], scene.takes[b]
    if ta.ruined_at(t) or tb.ruined_at(t):
        return CutCheck(False, RUINED)
    sa, sb = ta.states[t], tb.states[t]
    if sa.cell != sb.cell:
        return CutCheck(False, POSITION)
    if not (scene.grounded(sa) and scene.grounded(sb)):
        return CutCheck(False, AIRBORNE)
    if sa.carry != sb.carry:
        return CutCheck(False, PROP)
    return CutCheck(True)


@dataclass
class FilmResult:
    outcome: str  # "win" | "fail" | "illegal"
    tick: int
    take: int
    reason: str | None = None  # fail: hazard kind or "time"; illegal: CutCheck reason
    cuts_used: int = 0
    cut_index: int | None = None  # illegal: which cut
    shown: list[int] = field(default_factory=list)  # take on screen for each tick played
    ignored_cuts: int = 0  # cuts after the film already ended

    @property
    def won(self) -> bool:
        return self.outcome == "win"


def simulate_film(scene: Scene, opening: int, cuts: list[tuple[int, int]] | list[list[int]]) -> FilmResult:
    """Play the player's film: opening take on tick 0, then each cut (tick, target take) in order.

    Per tick: apply a cut due on this tick (an illegal one stops the film), then the shown take fails if the tick is in
    its mishap window, else wins if Lucky stands on the X mark (carrying the prop when the scene requires it).
    The film fails on time when the last tick passes without a win."""
    if not 0 <= opening < len(scene.takes):
        return FilmResult("illegal", 0, opening, SAME_TAKE)
    plan = [(int(t), int(b)) for t, b in cuts]
    cur, used, last, k = opening, 0, None, 0
    shown: list[int] = []
    for t in range(scene.ticks):
        if k < len(plan) and plan[k][0] < t:
            return FilmResult("illegal", plan[k][0], cur, SAME_TICK, used, k, shown, 0)
        if k < len(plan) and plan[k][0] == t:
            chk = is_legal_cut(scene, t, cur, plan[k][1], used, last)
            if not chk.ok:
                return FilmResult("illegal", t, cur, chk.reason, used, k, shown, 0)
            cur, used, last, k = plan[k][1], used + 1, t, k + 1
        shown.append(cur)
        take = scene.takes[cur]
        if take.ruined_at(t):
            s = take.states[min(t, len(take.states) - 1)]
            return FilmResult("fail", t, cur, scene.hazard_at(t, s.x, s.h) or "mishap", used, None, shown, len(plan) - k)
        if scene.at_goal(take.states[t]):
            return FilmResult("win", t, cur, None, used, None, shown, len(plan) - k)
    return FilmResult("fail", scene.ticks - 1, cur, "time", used, None, shown, len(plan) - k)
