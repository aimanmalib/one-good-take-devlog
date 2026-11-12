"""Seeded scene generator (PRD 4.5): lay out a stage, record takes around a safe path with detours into mishaps,
keep the candidates that pass every check, pick one per recipe, export JSON and log the statistics.

    uv run --frozen python tools/levels/generator.py --reel 1 --out Content/Data/scenes --stats devlog/data/levels.jsonl

Recipes (stage size, hazards, takes, target par, attempts, seed) live in `tools/levels/recipes.json`: change the
levels there, not here. The same recipe and seed always give the same scene.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from actor import STYLE, Plan, record  # noqa: E402
from checks import scene_problems  # noqa: E402
from model import Hazard, Prop, Scene, Take, load_rules, save_scene  # noqa: E402
from solver import count_wins, earliest_win, solve  # noqa: E402

HERE = Path(__file__).resolve().parent
RECIPES_PATH = HERE / "recipes.json"


def load_recipes(path: Path = RECIPES_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _span(rng: random.Random, pair: list[int]) -> int:
    return rng.randint(pair[0], pair[1])


def layout(recipe: dict, book: dict, rules: dict, rng: random.Random) -> Scene:
    """Stage width, crate, prop and standing-hazard columns (at least two columns apart, none on the first two columns
    or the last one), hazard cells for the longest allowed scene. Moving hazards take no column: they enter from the
    right edge (`moving_cells`)."""
    width = _span(rng, recipe["width"])
    book_hz = book["hazards"]
    placed = [k for k, n in sorted(recipe["hazards"].items()) if STYLE[k] != "moving" for _ in range(n)]
    kinds = placed + ["crate"] * recipe["crates"] + (["prop"] if recipe.get("prop") else [])
    rng.shuffle(kinds)
    free = list(range(2, width - 1))
    cols: list[int] = []
    for _ in kinds:
        options = [c for c in free if all(abs(c - o) >= 2 for o in cols)]
        if not options:
            raise ValueError("stage too narrow for the recipe")
        cols.append(rng.choice(options))
    cols.sort()
    ticks = rules["ticks"]["max"]
    crates, hazards, prop = set(), [], None
    for x, kind in zip(cols, kinds):
        if kind == "crate":
            crates.add(x)
        elif kind == "prop":
            prop = Prop(x, 0)
        elif STYLE[kind] == "static":
            hazards.append(Hazard(kind, {(t, x, 0) for t in range(ticks)}))
        else:
            cfg = book_hz[kind]
            period, phase = _span(rng, cfg["period"]), rng.randrange(8)
            on = [t for t in range(ticks) if (t + phase) % period < cfg["active"]]
            hazards.append(Hazard(kind, {(t, x, h) for t in on for h in cfg.get("levels", [0, 1])}))
    for kind, n in sorted(recipe["hazards"].items()):
        if STYLE[kind] == "moving":
            for _ in range(n):
                hazards.append(Hazard(kind, moving_cells(book_hz[kind], width, crates, ticks, rng)))
    return Scene(id=recipe["id"], title=recipe["title"], reel=recipe["reel"], width=width, ticks=ticks, budget=0,
                 takes=[], crates=crates, hazards=hazards, prop=prop, require_prop=bool(recipe.get("require_prop")))


def moving_cells(cfg: dict, width: int, crates: set[int], ticks: int, rng: random.Random) -> set:
    """Cells of a hazard entering at the right edge on tick `first` and travelling left `speed` columns per tick at
    level `h`, thrown again every `every` ticks when set. Each tick covers every column it sweeps through, so Lucky
    cannot slip past it between ticks. On the floor it stops against a crate."""
    cells: set[tuple[int, int, int]] = set()
    start = _span(rng, cfg["first"])
    every = _span(rng, cfg["every"]) if cfg.get("every") else None
    h, speed = cfg["h"], cfg["speed"]
    while start < ticks:
        prev = width - 1
        for t in range(start, ticks):
            x = width - 1 - speed * (t - start)
            swept = list(range(max(x, 0), prev + 1))
            wall = [c for c in swept if h == 0 and c in crates]
            if wall:
                swept = [c for c in swept if c > max(wall)]
            cells |= {(t, c, h) for c in swept}
            if x < 0 or wall:
                break
            prev = x
        if every is None:
            break
        start += every
    return cells


def plans(recipe: dict, book: dict, rules: dict, scene: Scene, rng: random.Random) -> list[Plan]:
    """One plan per take: `clean_takes` takes make no mistake, the others each get a different hazard wrong when
    there are enough hazards; random pauses and mishap lengths."""
    n_hz = len(scene.hazards)
    order = list(range(n_hz))
    rng.shuffle(order)
    out = []
    for i in range(recipe["takes"]):
        mistake = None if i < recipe["clean_takes"] or not n_hz else order[(i - recipe["clean_takes"]) % n_hz]
        span = None if rng.random() < book["permanent_chance"] else rng.randint(1, rules["mishap_max_ticks"])
        pauses: dict[int, int] = {}
        for _ in range(_span(rng, recipe["pauses"])):
            col = rng.randrange(0, scene.width - 1)
            pauses[col] = pauses.get(col, 0) + rng.randint(1, 2)
        out.append(Plan(mistake, span, pauses))
    if scene.prop is not None:
        carriers = _span(rng, recipe["prop_takes"])
        for p in rng.sample(out, min(carriers, len(out))):
            p.take_prop = True
    rng.shuffle(out)
    return out


def truncate(scene: Scene, ticks: int) -> None:
    """Cut the scene to `ticks` ticks; a mishap that would happen later no longer happens."""
    scene.ticks = ticks
    for hz in scene.hazards:
        hz.cells = {c for c in hz.cells if c[0] < ticks}
    for i, tk in enumerate(scene.takes):
        states = tk.states[:ticks]
        if tk.fail_tick is not None and tk.fail_tick >= ticks:
            scene.takes[i] = Take(states)
        else:
            scene.takes[i] = Take(states, tk.fail_tick, tk.recover_tick)


@dataclass
class Candidate:
    scene: Scene
    wins: int
    attempt: int

    def score(self) -> tuple:
        """Lower is better: the highest par the recipe allows, then the fewest winning films, then the first found."""
        return (-(self.scene.par or 0), self.wins, self.attempt)


def candidate(recipe: dict, book: dict, rules: dict, rng: random.Random, attempt: int) -> tuple[Candidate | None, str]:
    """Build one scene; returns (candidate, "") or (None, rejection reason)."""
    try:
        scene = layout(recipe, book, rules, rng)
    except ValueError:
        return None, "layout"
    scene.takes = [record(scene, p, scene.ticks) for p in plans(recipe, book, rules, scene, rng)]
    scene.budget = max(recipe["par"]) + recipe["budget_extra"]
    fastest = earliest_win(scene)
    if fastest is None:
        return None, "unsolvable"
    truncate(scene, max(rules["ticks"]["min"], min(rules["ticks"]["max"], fastest + 1 + _span(rng, recipe["slack"]))))
    film = solve(scene)
    if film is None:
        return None, "unsolvable"
    scene.par = len(film.cuts)
    if scene.par not in recipe["par"]:
        return None, f"par {scene.par}"
    scene.budget = scene.par + recipe["budget_extra"]
    scene.solution = solve(scene).as_dict()
    problems = scene_problems(scene, rules)
    if problems:
        return None, problems[0][:3]
    return Candidate(scene, count_wins(scene), attempt), ""


def generate(recipe: dict, book: dict, rules: dict | None = None) -> tuple[Scene | None, dict]:
    """Best scene for one recipe plus the statistics record (generated / rejected by reason / passed / kept)."""
    rules = rules or load_rules()
    rejected: Counter[str] = Counter()
    best: Candidate | None = None
    passed = 0
    attempts = recipe.get("attempts", book["attempts"])
    for attempt in range(attempts):
        rng = random.Random(f"{book['seed']}:{recipe['id']}:{attempt}")
        cand, why = candidate(recipe, book, rules, rng, attempt)
        if cand is None:
            rejected[why] += 1
            continue
        passed += 1
        if best is None or cand.score() < best.score():
            best = cand
    stats = {"scene": recipe["id"], "seed": book["seed"], "generated": attempts,
             "rejected": dict(sorted(rejected.items())), "passed": passed, "kept": 1 if best else 0}
    if best:
        stats |= {"par": best.scene.par, "budget": best.scene.budget, "winning_films": best.wins,
                  "ticks": best.scene.ticks, "width": best.scene.width, "attempt": best.attempt}
    return (best.scene if best else None), stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reel", type=int, required=True)
    ap.add_argument("--recipes", type=Path, default=RECIPES_PATH)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stats", type=Path, help="append one statistics line per scene (dev log levels.jsonl)")
    ap.add_argument("--slice", default=None, help="plan slice id recorded with the statistics")
    args = ap.parse_args(argv)
    book = load_recipes(args.recipes)
    reel = next((r for r in book["reels"] if r["reel"] == args.reel), None)
    if reel is None:
        print(f"no reel {args.reel} in {args.recipes.name}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    missing = 0
    for recipe in reel["scenes"]:
        scene, stats = generate(dict(recipe, reel=args.reel), book)
        print(json.dumps(stats))
        if scene is None:
            missing += 1
        else:
            save_scene(scene, args.out / f"{scene.id}.json")
        if args.stats:
            ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with args.stats.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "slice": args.slice, "reel": args.reel} | stats) + "\n")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
