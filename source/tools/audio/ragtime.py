"""Ragtime arranger (PRD 4.7): a stride left hand (bass on beats 1 and 3, chord on 2 and 4) and a syncopated right hand drawn
from rhythm cells, at 150 BPM so one beat is one game tick (0.4 s). Songs are loops of whole bars: the tail of the last
notes wraps onto the start, so the music can loop forever and every ROLL can start it on tick 0.

Songs, chords, rhythm cells and voicing values all live in audio.json.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import dsp
import piano

MAJOR = (0, 2, 4, 5, 7, 9, 11)


@dataclass(frozen=True)
class Note:
    beat: float      # start, in beats from the song start
    midi: int
    velocity: float
    beats: float     # held length in beats
    hand: str        # "lh" or "rh"


def bar_chords(spec: str) -> list[tuple[str, float, float]]:
    """'II7:V7' -> [('II7', 0, 2), ('V7', 2, 2)]; 'I' -> [('I', 0, 4)] (start beat, length in beats)."""
    per = dsp.config()["beats_per_bar"]
    names = spec.split(":")
    step = per / len(names)
    return [(nm, i * step, step) for i, nm in enumerate(names)]


def left_hand(key: int, chord: list[int], start: float, beats: float, vel: float) -> list[Note]:
    st = dsp.config()["stride"]
    root = key + chord[0] + 12 * st["bass_octave"]
    fifth = key + chord[2 if len(chord) > 2 else 0] + 12 * st["bass_octave"]
    if fifth - root > 7:
        fifth -= 12
    voicing = [key + c + 12 * st["chord_octave"] for c in chord[-3:]]
    out = []
    for b in range(int(beats)):
        at = start + b
        if b % 2 == 0:
            bass = root if b == 0 or beats < 4 else fifth
            out.append(Note(at, bass, vel * st["bass_velocity"], 0.9, "lh"))
            out.append(Note(at, bass - 12, vel * st["bass_velocity"] * 0.6, 0.9, "lh"))
        else:
            out += [Note(at, m, vel * st["chord_velocity"], 0.55, "lh") for m in voicing]
    return out


def right_hand(name: str, key: int, chords: list, bar: int, cell: list, vel: float, prev: int) -> tuple[list[Note], int]:
    cfg = dsp.config()
    mel = cfg["melody"]
    g = dsp.rng(f"melody:{name}:{bar}")
    centre = key + 12 * mel["octave"]
    out = []
    for pos, length in cell:
        beat = pos / 2.0
        chord = next(c for nm, s, ln in chords for c in [cfg["chords"][nm]] if s <= beat < s + ln)
        tones = {key + 12 * o + c for o in range(0, 3) for c in chord}
        if pos % 2:  # off-beats may pass through the scale
            tones |= {key + 12 * o + s for o in range(0, 3) for s in MAJOR}
        cand = sorted(t for t in tones if abs(t - prev) <= mel["max_step"] and abs(t - centre) <= 12 and t != prev)
        if not cand or g.random() < mel["rest_chance"]:
            continue
        w = np.array([1.0 / (1 + abs(t - prev)) + (0.4 if (t - centre) * (prev - centre) < 0 else 0) for t in cand])
        pick = int(cand[int(g.choice(len(cand), p=w / w.sum()))])
        accent = 1.0 if pos % 2 else 0.85  # syncopation: the off-beat is the strong one
        out.append(Note(bar * 4 + beat, pick, vel * accent, length / 2.0 * 0.9, "rh"))
        if length >= 2:  # long syncopated notes get a sixth or third underneath, as in a rag
            under = max((t for t in tones if t < pick - 2 and t > pick - 10), default=None)
            if under is not None:
                out.append(Note(bar * 4 + beat, int(under), vel * accent * 0.6, length / 2.0 * 0.9, "rh"))
        prev = pick
    return out, prev


def score(name: str) -> list[Note]:
    cfg = dsp.config()
    song = cfg["songs"][name]
    key, vel = song["key_midi"], song["velocity"]
    notes: list[Note] = []
    prev = key + 12 * cfg["melody"]["octave"] + 4
    for bar, spec in enumerate(song["progression"]):
        chords = bar_chords(spec)
        for nm, s, ln in chords:
            notes += left_hand(key, cfg["chords"][nm], bar * 4 + s, ln, vel)
        cell = cfg["rhythm_cells"][song["rh_cells"][bar % len(song["rh_cells"])]]
        if bar == len(song["progression"]) - 1:
            cell = [[0, 4], [4, 1], [6, 1]]  # final bar: land on the tonic, two pick-ups lead back into the loop
        rh, prev = right_hand(name, key, chords, bar, cell, vel, prev)
        notes += rh
    return sorted(notes, key=lambda n: (n.beat, n.midi))


def song_beats(name: str) -> int:
    return len(dsp.config()["songs"][name]["progression"]) * dsp.config()["beats_per_bar"]


def render_notes(notes: list[Note], total_beats: int, loop: bool) -> np.ndarray:
    bs = dsp.beat_s()
    buf = np.zeros(int(round(total_beats * bs * dsp.sr())))
    for n in notes:
        piano.place(buf, piano.note(n.midi, round(n.velocity, 3), round(n.beats * bs, 4)), n.beat * bs, loop)
    return buf


def render(name: str) -> np.ndarray:
    return render_notes(score(name), song_beats(name), loop=True)
