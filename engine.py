"""Real-time engine: one captured stream in, one independently clocked stream
out per speaker, kept aligned with per-speaker delay buffers and adaptive
resampling (every Bluetooth speaker runs on its own clock)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

import dsp


@dataclass
class SpeakerSettings:
    pos: tuple = (0.0, 1.0, 1.2)
    volume: float = 1.0
    mute: bool = False
    trim_ms: float = 0.0
    lo: float = 20.0
    hi: float = 20000.0


@dataclass
class MixSettings:
    mode: str = "sync"              # "surround" | "frequency" | "sync"
    master: float = 1.0
    base_ms: float = 100.0
    listener: tuple = (0.0, 0.0, 1.2)
    focus: float = 2.0
    delay_comp: bool = True
    level_comp: bool = True
    upmix: bool = True
    speakers: list = field(default_factory=list)


class SpeakerOut:
    """Delay line + drift-correcting resampler feeding one output device."""

    CAP_S = 4.0

    def __init__(self, name, src_rate, dev_rate):
        self.name = name
        self.src_rate = src_rate
        self.base_ratio = src_rate / dev_rate
        self.cap = int(src_rate * self.CAP_S)
        self.buf = np.zeros((self.cap, 2), np.float32)
        self.w = 0            # frames written (total)
        self.r = 0            # read position, integer part
        self.frac = 0.0       # read position, fractional part
        self.t_w = 0.0
        self.primed = False
        self.err = 0.0
        self.target_s = 0.1
        self.volume = 1.0
        self.mute = False
        self.peak = 0.0
        self.underruns = 0
        self.beep = None
        self.lock = threading.Lock()

    def write(self, x):
        n = len(x)
        with self.lock:
            i = self.w % self.cap
            k = min(n, self.cap - i)
            self.buf[i:i + k] = x[:k]
            if k < n:
                self.buf[:n - k] = x[k:]
            self.w += n
            self.t_w = time.perf_counter()

    def _read(self, n_out):
        rate = self.src_rate
        silence = np.zeros((n_out, 2), np.float32)
        with self.lock:
            # Smooth estimate of how much audio has "arrived" by now.
            age = min(max(time.perf_counter() - self.t_w, 0.0), 0.03)
            w_est = self.w + age * rate
            floor = (self.base_ratio * 1.01 * n_out + 3) * 1.5
            target = max(self.target_s * rate, floor)
            fill = w_est - (self.r + self.frac)
            if not self.primed:
                if fill < target:
                    return silence
                self.r, self.frac, self.err = int(w_est - target), 0.0, 0.0
                self.primed = True
                fill = target
            err = fill - target
            if abs(err) > 0.04 * rate:      # far off (e.g. delay changed): jump
                self.r = max(int(w_est - target), self.w - self.cap + int(floor), 0)
                self.frac, self.err, err = 0.0, 0.0, 0.0
            self.err += 0.05 * (err - self.err)
            ratio = self.base_ratio * (1.0 + float(np.clip(0.5 * self.err / rate, -0.005, 0.005)))
            p = self.frac + ratio * np.arange(n_out)
            i0 = p.astype(np.int64)
            need = int(i0[-1]) + 2
            if self.w - self.r < need:
                self.primed = False
                self.r, self.frac = self.w, 0.0
                self.underruns += 1
                return silence
            chunk = self.buf[(self.r + np.arange(need)) % self.cap]
            a = (p - i0).astype(np.float32)[:, None]
            out = chunk[i0] * (1.0 - a) + chunk[i0 + 1] * a
            end = self.frac + ratio * n_out
            adv = int(end)
            self.r += adv
            self.frac = end - adv
            return out

    def pull(self, n_out):
        out = self._read(n_out)
        out = out * (0.0 if self.mute else self.volume)
        beep = self.beep
        if beep is not None:
            k = min(len(beep), n_out)
            out[:k] += beep[:k, None]
            self.beep = beep[k:] if k < len(beep) else None
        pk = float(np.max(np.abs(out))) if n_out else 0.0
        if pk > self.peak:
            self.peak = pk
        return np.clip(out, -1.0, 1.0).astype(np.float32)


class Engine:
    def __init__(self, backend):
        self.backend = backend
        self.running = False
        self.outs: list[SpeakerOut] = []
        self.plan = None
        self.click_on = False
        self._streams = []
        self._filters = []
        self.src_rate = 48000
        self.n_in = 2

    # ---- lifecycle ---------------------------------------------------
    def start(self, source: dict, outputs: list, settings: MixSettings):
        self.stop()
        self.src_rate = int(source["rate"])
        self.n_in = int(source["channels"])
        self.outs = [SpeakerOut(o["name"], self.src_rate, int(o["rate"])) for o in outputs]
        self._dev_rates = [int(o["rate"]) for o in outputs]
        self._filters = [None] * len(self.outs)
        self.apply(settings)
        self.running = True
        try:
            for o, dev in zip(self.outs, outputs):
                self._streams.append(self.backend.open_output(
                    dev["index"], int(dev["rate"]), int(dev["channels"]), o.pull))
            self._streams.append(self.backend.open_capture(
                source["index"], self.src_rate, self.n_in, self._on_capture))
        except Exception:
            self.stop()
            raise
        threading.Thread(target=self._click_loop, daemon=True).start()

    def stop(self):
        self.running = False
        for s in self._streams:
            try:
                self.backend.close(s)
            except Exception:
                pass
        self._streams = []
        self.outs = []
        self.plan = None

    # ---- configuration (safe to call while running) -------------------
    def apply(self, st: MixSettings):
        n = len(self.outs)
        if n == 0 or len(st.speakers) != n:
            return
        trims = np.array([s.trim_ms / 1000.0 for s in st.speakers])
        comp = np.zeros(n)
        M = None
        if st.mode == "surround":
            pos = [s.pos for s in st.speakers]
            az, U = dsp.bed(self.n_in, st.upmix)
            G = dsp.pan_gains(pos, st.listener, az, st.focus)
            dist = dsp.distances(pos, st.listener)
            if st.level_comp:
                G = G * np.clip(dist / dist.max(), 0.1, 1.0)[:, None]
            if st.delay_comp:
                comp = (dist.max() - dist) / dsp.SPEED_OF_SOUND
            M = G @ U
            # Headroom: correlated channels can add up, so keep the worst case <= 1.
            M = (M / max(1.0, float(np.abs(M).sum(axis=1).max()))).astype(np.float32)
        elif st.mode == "frequency":
            for i, s in enumerate(st.speakers):
                f = self._filters[i]
                if f is None or f.key != (s.lo, s.hi):
                    self._filters[i] = dsp.BandFilter(s.lo, s.hi, self.src_rate)
        total = comp + trims
        total -= total.min()
        for o, s, t in zip(self.outs, st.speakers, total):
            o.target_s = min(st.base_ms / 1000.0 + float(t), SpeakerOut.CAP_S - 1.0)
            o.volume = st.master * s.volume
            o.mute = s.mute
        self.plan = (st.mode, M, dsp.downmix(self.n_in))

    # ---- audio path --------------------------------------------------
    def _on_capture(self, block):
        if not self.click_on:
            self._process(block)

    def _process(self, block):
        plan, outs = self.plan, self.outs
        if plan is None or not outs or block.shape[1] != self.n_in:
            return
        mode, M, D = plan
        if mode == "surround":
            mono = block @ M.T
            for i, o in enumerate(outs):
                o.write(np.repeat(mono[:, i:i + 1], 2, axis=1))
        else:
            stereo = block @ D.T
            for i, o in enumerate(outs):
                f = self._filters[i] if mode == "frequency" else None
                o.write(f.process(stereo) if f is not None else stereo)

    def _click_loop(self):
        rate = self.src_rate
        blk = rate // 100
        t = np.arange(int(rate * 0.006)) / rate
        burst = (0.7 * np.sin(2 * np.pi * 1500 * t) * np.hanning(len(t))).astype(np.float32)
        n, t0 = 0, time.perf_counter()
        while self.running:
            if not self.click_on:
                time.sleep(0.05)
                n, t0 = 0, time.perf_counter()
                continue
            x = np.zeros(blk, np.float32)
            ph = n % rate
            if ph < len(burst):
                k = min(blk, len(burst) - ph)
                x[:k] = burst[ph:ph + k]
            self._process(np.repeat(x[:, None], self.n_in, axis=1))
            n += blk
            time.sleep(max(0.0, t0 + n / rate - time.perf_counter()))

    # ---- utilities ---------------------------------------------------
    def beep(self, i):
        if 0 <= i < len(self.outs):
            rate = self._dev_rates[i]
            t = np.arange(int(rate * 0.5)) / rate
            env = np.minimum(1, np.minimum(t, 0.5 - t) / 0.02)
            self.outs[i].beep = (0.35 * np.sin(2 * np.pi * 660 * t) * env).astype(np.float32)

    def levels(self):
        lv = []
        for o in self.outs:
            lv.append(o.peak)
            o.peak = 0.0
        return lv

    def underruns(self):
        return sum(o.underruns for o in self.outs)
