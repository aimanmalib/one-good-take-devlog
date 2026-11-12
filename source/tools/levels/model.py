"""Scene data model (PRD 4.4): stage, takes, hazards, prop; JSON load/save; motion rules.

Everything is discrete. A stage is `width` columns and two levels (h = 0 floor, h = 1 on a crate or in the air).
Time is counted in ticks (one music beat). A take is a fixed recording of Lucky's state on every tick. All takes start
in the same cell on tick 0 and run at the same time; the hazards are the same for every take.

The mishap window (ADR 0009): a take is ruined from `fail_tick` until `recover_tick` (exclusive). `recover_tick = None`
means ruined for the rest of the recording, which is the PRD's literal rule.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE.parent.parent / "Content" / "Data" / "rules.json"
SCENE_SCHEMA = 1


def load_rules(path: Path = RULES_PATH) -> dict:
    """Shared limits and vocabularies (also read by the game runtime)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class State:
    x: int
    h: int
    carry: bool = False
    pose: str = "stand"

    @property
    def cell(self) -> tuple[int, int]:
        return (self.x, self.h)


@dataclass
class Take:
    states: list[State]
    fail_tick: int | None = None
    recover_tick: int | None = None

    def ruined_at(self, t: int) -> bool:
        """True when tick t is inside this take's mishap window (or past the end of its recording)."""
        if t >= len(self.states):
            return True
        if self.fail_tick is None or t < self.fail_tick:
            return False
        return self.recover_tick is None or t < self.recover_tick


@dataclass
class Hazard:
    kind: str
    cells: set[tuple[int, int, int]]  # (tick, x, h)


@dataclass
class Prop:
    x: int
    h: int


@dataclass
class Scene:
    id: str
    width: int
    ticks: int
    budget: int
    takes: list[Take]
    crates: set[int] = field(default_factory=set)
    hazards: list[Hazard] = field(default_factory=list)
    prop: Prop | None = None
    require_prop: bool = False
    title: str = ""
    reel: int = 1
    par: int | None = None
    solution: dict | None = None  # {"opening": int, "cuts": [[tick, take], ...]}

    @property
    def goal_x(self) -> int:
        return self.width - 1

    def grounded(self, s: State) -> bool:
        """Standing on the floor (h 0, no crate) or on top of a crate (h 1 over a crate)."""
        on_crate = s.x in self.crates
        return (s.h == 1) == on_crate

    def valid_cell(self, x: int, h: int) -> bool:
        return 0 <= x < self.width and h in (0, 1) and not (h == 0 and x in self.crates)

    def hazard_at(self, t: int, x: int, h: int) -> str | None:
        for hz in self.hazards:
            if (t, x, h) in hz.cells:
                return hz.kind
        return None

    def first_hit(self, take: Take, start: int = 0) -> int | None:
        for t in range(start, len(take.states)):
            s = take.states[t]
            if self.hazard_at(t, s.x, s.h):
                return t
        return None

    def at_goal(self, s: State) -> bool:
        return s.x == self.goal_x and self.grounded(s) and (s.carry or not self.require_prop)


# ---------------------------------------------------------------- motion rules (PRD 4.5 f)

def move_kind(scene: Scene, a: State, b: State) -> str | None:
    """Name of the move from a to b on the next tick, or None when it breaks the motion rules.

    run/wait on the same level, climb onto a crate, step down from one, jump (grounded -> air, at most one column),
    land (air -> grounded, at most one column). A leap over one cell is jump + land."""
    if not scene.valid_cell(b.x, b.h):
        return None
    dx = b.x - a.x
    if abs(dx) > 1:
        return None
    ga, gb = scene.grounded(a), scene.grounded(b)
    if not ga:
        return "land" if gb else None
    if not gb:
        return "jump"
    if a.h == b.h:
        return "wait" if dx == 0 else "run"
    return "climb" if b.h > a.h else "down"


def motion_errors(scene: Scene, take: Take) -> list[str]:
    """Every rule the recording breaks (empty = plausible take). Inside the mishap window Lucky is knocked about:
    only cell validity and one column per tick are checked there."""
    errs: list[str] = []
    st = take.states
    if not st:
        return ["empty take"]
    if not scene.grounded(st[0]) or st[0].carry:
        errs.append("tick 0: must start grounded and empty-handed")
    for t, s in enumerate(st):
        if not scene.valid_cell(s.x, s.h):
            errs.append(f"tick {t}: cell ({s.x},{s.h}) is outside the stage or inside a crate")
    for t in range(1, len(st)):
        a, b = st[t - 1], st[t]
        dazed = take.fail_tick is not None and take.fail_tick < t and take.ruined_at(t - 1)
        if dazed:
            if abs(b.x - a.x) > 1:
                errs.append(f"tick {t}: knocked more than one column")
        elif move_kind(scene, a, b) is None:
            errs.append(f"tick {t}: ({a.x},{a.h}) -> ({b.x},{b.h}) is not run, wait, jump, land, climb or down")
        if a.carry and not b.carry:
            errs.append(f"tick {t}: the prop cannot be dropped")
        if b.carry and not a.carry and (scene.prop is None or (b.x, b.h) != (scene.prop.x, scene.prop.h)):
            errs.append(f"tick {t}: picked up the prop away from its cell")
    return errs


def take_errors(scene: Scene, i: int, rules: dict) -> list[str]:
    """Structure + mishap window + motion for take i."""
    take = scene.takes[i]
    errs = [f"take {i}: {e}" for e in motion_errors(scene, take)]
    poses = set(rules["poses"])
    errs += [f"take {i}: tick {t}: unknown pose {s.pose!r}" for t, s in enumerate(take.states) if s.pose not in poses]
    hit = scene.first_hit(take)
    if hit != take.fail_tick:
        errs.append(f"take {i}: fail_tick {take.fail_tick} but first hazard hit is {hit}")
    if take.fail_tick is None:
        if take.recover_tick is not None:
            errs.append(f"take {i}: recover_tick without fail_tick")
        if len(take.states) != scene.ticks:
            errs.append(f"take {i}: {len(take.states)} states, expected {scene.ticks}")
        return errs
    if take.recover_tick is None:
        if len(take.states) != take.fail_tick + 1:
            errs.append(f"take {i}: a ruined take ends on its fail tick ({take.fail_tick})")
        return errs
    span = take.recover_tick - take.fail_tick
    if not 1 <= span <= rules["mishap_max_ticks"]:
        errs.append(f"take {i}: mishap lasts {span} ticks (1..{rules['mishap_max_ticks']})")
    if len(take.states) != scene.ticks:
        errs.append(f"take {i}: {len(take.states)} states, expected {scene.ticks}")
    elif take.recover_tick < scene.ticks:
        if not scene.grounded(take.states[take.recover_tick]):
            errs.append(f"take {i}: must be standing on recover tick {take.recover_tick}")
        again = scene.first_hit(take, take.recover_tick)
        if again is not None:
            errs.append(f"take {i}: second hazard hit at tick {again} (one mishap per take)")
    return errs


def scene_errors(scene: Scene, rules: dict | None = None) -> list[str]:
    """Structural validity (not puzzle quality: that is check.py, P6)."""
    rules = rules or load_rules()
    errs: list[str] = []
    w, n = rules["width"], rules["ticks"]
    if not w["min"] <= scene.width <= w["max"]:
        errs.append(f"width {scene.width} outside {w['min']}..{w['max']}")
    if not n["min"] <= scene.ticks <= n["max"]:
        errs.append(f"ticks {scene.ticks} outside {n['min']}..{n['max']}")
    if not 1 <= len(scene.takes) <= rules["max_takes"]:
        errs.append(f"{len(scene.takes)} takes (1..{rules['max_takes']})")
    if scene.budget < 0:
        errs.append("negative budget")
    if any(not 0 <= c < scene.width for c in scene.crates) or scene.goal_x in scene.crates:
        errs.append("crate outside the stage or on the X mark")
    if scene.require_prop and scene.prop is None:
        errs.append("require_prop without a prop")
    if scene.prop is not None and not scene.valid_cell(scene.prop.x, scene.prop.h):
        errs.append("prop in an invalid cell")
    kinds = set(rules["hazard_kinds"])
    errs += [f"unknown hazard kind {hz.kind!r}" for hz in scene.hazards if hz.kind not in kinds]
    starts = {tk.states[0].cell for tk in scene.takes if tk.states}
    if len(starts) > 1:
        errs.append("takes do not start in the same cell")
    for i in range(len(scene.takes)):
        errs += take_errors(scene, i, rules)
    return errs


# ---------------------------------------------------------------- JSON

def scene_from_dict(d: dict) -> Scene:
    if d.get("schema") != SCENE_SCHEMA:
        raise ValueError(f"scene schema {d.get('schema')!r}, expected {SCENE_SCHEMA}")
    takes = [Take(states=[State(x, h, bool(c), p) for x, h, c, p in tk["states"]],
                  fail_tick=tk.get("fail_tick"), recover_tick=tk.get("recover_tick")) for tk in d["takes"]]
    hazards = [Hazard(hz["kind"], {tuple(c) for c in hz["cells"]}) for hz in d.get("hazards", [])]
    prop = Prop(**d["prop"]) if d.get("prop") else None
    return Scene(id=d["id"], width=d["width"], ticks=d["ticks"], budget=d["budget"], takes=takes,
                 crates=set(d.get("crates", [])), hazards=hazards, prop=prop,
                 require_prop=bool(d.get("require_prop", False)), title=d.get("title", ""),
                 reel=d.get("reel", 1), par=d.get("par"), solution=d.get("solution"))


def scene_to_dict(s: Scene) -> dict:
    return {
        "schema": SCENE_SCHEMA, "id": s.id, "reel": s.reel, "title": s.title,
        "width": s.width, "ticks": s.ticks, "budget": s.budget, "par": s.par,
        "crates": sorted(s.crates),
        "prop": {"x": s.prop.x, "h": s.prop.h} if s.prop else None,
        "require_prop": s.require_prop,
        "hazards": [{"kind": hz.kind, "cells": [list(c) for c in sorted(hz.cells)]} for hz in s.hazards],
        "takes": [{"fail_tick": tk.fail_tick, "recover_tick": tk.recover_tick,
                   "states": [[st.x, st.h, int(st.carry), st.pose] for st in tk.states]} for tk in s.takes],
        "solution": s.solution,
    }


def load_scene(path: Path) -> Scene:
    return scene_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def save_scene(scene: Scene, path: Path) -> None:
    Path(path).write_text(json.dumps(scene_to_dict(scene), indent=1) + "\n", encoding="utf-8")
