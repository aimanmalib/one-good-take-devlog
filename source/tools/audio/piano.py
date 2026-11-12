"""Honky-tonk piano voice (PRD 4.7, Amendment A6): each note is two or three slightly mistuned strings, each an additive stack
of stretched partials (string inharmonicity) with a two-stage decay, plus a short felt-hammer thump.

Mistuning: every pitch has a few string sets (piano.strings_cents) plus a small seeded jitter, so the same key sounds the same
each time it is struck (like one real out-of-tune piano), while neighbouring keys beat differently.
"""
from __future__ import annotations

from functools import cache

import numpy as np

import dsp


def _cfg() -> dict:
    return dsp.config()["piano"]


@cache
def string_offsets(midi: int) -> tuple[float, ...]:
    c = _cfg()
    sets = c["strings_cents"]
    g = dsp.rng(f"piano-key:{midi}")
    base = sets[int(g.integers(len(sets)))]
    n = 2 if midi >= 84 else 3  # the top octave has fewer strings sounding out of tune
    jitter = g.uniform(-c["string_jitter_cents"], c["string_jitter_cents"], n)
    return tuple(float(base[i] + jitter[i]) for i in range(n))


@cache
def note(midi: int, velocity: float, hold_s: float) -> np.ndarray:
    """One struck note held for hold_s, then damped over release_s. Cached: arrangements reuse the same notes."""
    c = _cfg()
    fs = dsp.sr()
    tail = 0.25 + c["release_s"]
    n = int((hold_s + tail) * fs)
    t = np.arange(n) / fs
    f0 = dsp.midi_hz(midi)
    decay = c["decay_s_at_c4"] * (dsp.midi_hz(60) / f0) ** c["decay_pitch_slope"]
    bright = 0.6 + c["brightness_per_velocity"] * velocity
    out = np.zeros(n)
    k = np.arange(1, c["partials"] + 1)
    amps = (1.0 / k ** c["partial_rolloff"]) * np.exp(-(k - 1) / (3.0 * bright))
    nyq = fs / 2 - 200
    for cents in string_offsets(midi):
        fs_k = f0 * 2 ** (cents / 1200) * k * np.sqrt(1 + c["inharmonicity"] * k * k)
        ph = dsp.rng(f"piano-phase:{midi}:{cents:.3f}").uniform(0, 2 * np.pi, len(k))
        for fk, ak, pk in zip(fs_k, amps, ph):
            if fk >= nyq:
                break
            tau = decay / (1 + 0.35 * (fk / f0 - 1))
            env = 0.65 * np.exp(-t / (tau * 0.18)) + 0.35 * np.exp(-t / tau)
            out += ak * env * np.sin(2 * np.pi * fk * t + pk)
    out /= len(string_offsets(midi))
    # felt hammer: a few ms of low-passed noise at the attack
    hn = int(c["hammer_ms"] / 1000 * fs)
    thump = dsp.rng(f"hammer:{midi}").standard_normal(hn) * np.exp(-np.linspace(0, 6, hn))
    thump = np.convolve(thump, np.ones(12) / 12, mode="same")
    out[:hn] += c["hammer_level"] * velocity * thump
    # attack ramp (2 ms) and damper release
    att = int(0.002 * fs)
    out[:att] *= np.linspace(0, 1, att)
    rel_start = int(hold_s * fs)
    rel_n = int(c["release_s"] * fs)
    out[rel_start:rel_start + rel_n] *= np.linspace(1, 0, rel_n) ** 2
    out[rel_start + rel_n:] = 0.0
    out.setflags(write=False)
    return out * velocity


def place(buf: np.ndarray, sig: np.ndarray, at_s: float, loop: bool) -> None:
    """Add sig into buf starting at at_s; on a loop the tail wraps to the start so the seam is seamless."""
    i = int(round(at_s * dsp.sr()))
    n = len(buf)
    end = min(n, i + len(sig))
    buf[i:end] += sig[: end - i]
    rest = sig[end - i:]
    if loop and len(rest):
        buf[: len(rest)] += rest[:n]
