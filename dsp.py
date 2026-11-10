"""Signal-processing helpers: surround panning, downmixing, band filters."""
from __future__ import annotations

import numpy as np
from scipy import signal

SPEED_OF_SOUND = 343.0  # m/s


def bed(n_in: int, upmix: bool):
    """Describe the virtual surround channels for an input with n_in channels.

    Returns (azimuths, U). azimuths[i] is the direction of virtual channel i in
    degrees clockwise from straight ahead (None = non-directional, e.g. LFE).
    U maps input channels to virtual channels (n_virtual x n_in).
    """
    if n_in == 1:
        return [0.0], np.eye(1, dtype=np.float32)
    if n_in == 2 and upmix:
        # Passive matrix upmix: centre from the sum, rears from the difference.
        U = np.array([[1, 0], [0, 1], [0.5, 0.5], [0.7, -0.35], [-0.35, 0.7]], np.float32)
        return [-30.0, 30.0, 0.0, -110.0, 110.0], U
    if n_in == 4:
        az = [-30.0, 30.0, -110.0, 110.0]
    elif n_in == 6:   # FL FR C LFE BL BR
        az = [-30.0, 30.0, 0.0, None, -110.0, 110.0]
    elif n_in == 8:   # FL FR C LFE BL BR SL SR
        az = [-30.0, 30.0, 0.0, None, -150.0, 150.0, -90.0, 90.0]
    else:
        az = [-30.0, 30.0] + [None] * (n_in - 2)
    return az, np.eye(n_in, dtype=np.float32)


def pan_gains(positions, listener, azimuths, focus: float) -> np.ndarray:
    """Direction-based amplitude panning.

    Each virtual channel is shared between the speakers that lie closest to its
    direction as seen from the listener. Returns gains (n_speakers x n_virtual),
    power-normalised per channel.
    """
    pos = np.asarray(positions, float).reshape(-1, 3)
    v = pos - np.asarray(listener, float)
    d = np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 0.1)
    s = v / d
    n = len(pos)
    G = np.zeros((n, len(azimuths)), np.float32)
    for c, az in enumerate(azimuths):
        if az is None:
            w = np.ones(n)
        else:
            a = np.radians(az)
            dots = s @ np.array([np.sin(a), np.cos(a), 0.0])
            w = np.clip(dots, 0.0, 1.0) ** focus
            if w.max() < 1e-6:          # nobody is on that side: use the nearest
                w = np.zeros(n)
                w[int(np.argmax(dots))] = 1.0
        G[:, c] = w / np.sqrt(np.sum(w * w))
    return G


def distances(positions, listener) -> np.ndarray:
    v = np.asarray(positions, float).reshape(-1, 3) - np.asarray(listener, float)
    return np.maximum(np.linalg.norm(v, axis=1), 0.1)


def downmix(n_in: int) -> np.ndarray:
    """Stereo downmix matrix (2 x n_in)."""
    if n_in == 1:
        D = [[1.0], [1.0]]
    elif n_in == 2:
        D = [[1, 0], [0, 1]]
    elif n_in == 4:
        D = np.array([[1, 0, .7, 0], [0, 1, 0, .7]]) / 1.4
    elif n_in == 6:
        D = np.array([[1, 0, .707, .5, .707, 0], [0, 1, .707, .5, 0, .707]]) / 2.0
    elif n_in == 8:
        D = np.array([[1, 0, .707, .5, .707, 0, .707, 0],
                      [0, 1, .707, .5, 0, .707, 0, .707]]) / 2.4
    else:
        D = np.zeros((2, n_in))
        D[0, 0] = D[1, 1] = 1.0
    return np.asarray(D, np.float32)


class BandFilter:
    """Stateful 4th-order Butterworth band filter for stereo blocks."""

    def __init__(self, lo: float, hi: float, fs: float):
        self.key = (lo, hi)
        nyq = fs / 2
        lo_on = lo > 25
        hi_on = hi < min(19500, nyq * 0.95)
        hi = min(hi, nyq * 0.95)
        if lo_on and hi_on and hi > lo:
            self.sos = signal.butter(4, [lo, hi], "bandpass", fs=fs, output="sos")
        elif lo_on:
            self.sos = signal.butter(4, min(lo, nyq * 0.95), "highpass", fs=fs, output="sos")
        elif hi_on:
            self.sos = signal.butter(4, hi, "lowpass", fs=fs, output="sos")
        else:
            self.sos = None
        if self.sos is not None:
            self.zi = np.zeros((self.sos.shape[0], 2, 2))

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.sos is None:
            return x
        y, self.zi = signal.sosfilt(self.sos, x, axis=0, zi=self.zi)
        return y.astype(np.float32)


def auto_bands(n: int):
    edges = np.geomspace(20, 20000, n + 1)
    edges = [int(round(e, -1)) if e >= 100 else int(round(e)) for e in edges]
    return [(edges[i], edges[i + 1]) for i in range(n)]
