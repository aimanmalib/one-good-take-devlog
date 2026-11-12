"""The "old projector speaker" chain (Amendment A6) that every stem goes through: slow wow plus a little flutter (a
time-varying read position), a light tanh saturation, a band limit of roughly 200 to 5000 Hz, and a faint hiss (hiss_db under the stem's RMS).
Values live in audio.json under projector_chain.
"""
from __future__ import annotations

import numpy as np

import dsp


def wow(x: np.ndarray, name: str, loop: bool) -> np.ndarray:
    """Pitch wobble: read the signal at t + d(t). For loops the wobble rates are rounded to whole cycles per loop, so the
    seam stays continuous."""
    c = dsp.config()["projector_chain"]
    fs, n = dsp.sr(), len(x)
    dur = n / fs
    t = np.arange(n) / fs
    ph = dsp.rng("wow:" + name).uniform(0, 2 * np.pi, 2)
    rates = [c["wow_hz"], c["flutter_hz"]]
    if loop:
        rates = [max(1, round(r * dur)) / dur for r in rates]
    # depth is the peak pitch deviation (fraction); delay amplitude = depth / (2 pi f)
    d = sum(c[k] / (2 * np.pi * r) * np.sin(2 * np.pi * r * t + p)
            for k, r, p in zip(("wow_depth", "flutter_depth"), rates, ph))
    pos = np.arange(n) + d * fs
    if loop:
        return np.interp(pos % n, np.arange(n + 1), np.concatenate([x, x[:1]]))
    return np.interp(pos, np.arange(n), x, left=0.0, right=0.0)


def saturate(x: np.ndarray) -> np.ndarray:
    drive = dsp.config()["projector_chain"]["drive"]
    peak = np.abs(x).max() or 1.0
    y = np.tanh(drive * x / peak) / np.tanh(drive)
    return y * peak


def band(x: np.ndarray, loop: bool) -> np.ndarray:
    c = dsp.config()["projector_chain"]
    return dsp.apply_mag(x, lambda f: dsp.butter_mag(f, c["highpass_hz"], c["lowpass_hz"], c["filter_order"]), loop)


def hiss(n: int, name: str, level_rms: float) -> np.ndarray:
    c = dsp.config()["projector_chain"]
    noise = dsp.rng("hiss:" + name).standard_normal(n)
    return noise * level_rms * 10 ** (c["hiss_db"] / 20)


def projector(x: np.ndarray, name: str, loop: bool = False) -> np.ndarray:
    """Run one stem through the whole chain. The hiss sits a fixed distance under the stem's own RMS and is band limited
    with it; it only rides under the sound (one-shots fade with their envelope)."""
    y = saturate(wow(x, name, loop))
    rms = float(np.sqrt(np.mean(y * y))) or 1.0
    env = np.abs(y)
    if not loop:
        k = max(1, dsp.sr() // 50)
        env = np.convolve(env, np.ones(k) / k, mode="same")
        env = env / (env.max() or 1.0)
    else:
        env = np.ones_like(y)
    y = y + hiss(len(y), name, rms) * env
    return band(y, loop)
