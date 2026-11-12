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


def beep_tone(rate, seconds=0.5, freq=660.0):
    t = np.arange(int(rate * seconds)) / rate
    env = np.minimum(1, np.minimum(t, seconds - t) / 0.02)
    return (0.35 * np.sin(2 * np.pi * freq * t) * env).astype(np.float32)


TONE_LEAD_S = 0.35      # Bluetooth speakers often swallow the first moment after waking


def open_tone_stream(backend, dev):
    """Open the device on its own and play the identify tone once (used while
    playback is stopped). The caller closes the stream after TONE_TOTAL_S."""
    rate = int(dev["rate"])
    tone = np.concatenate([np.zeros(int(rate * TONE_LEAD_S), np.float32), beep_tone(rate)])
    pos = [0]

    def pull(n):
        out = np.zeros((n, 2), np.float32)
        k = max(0, min(n, len(tone) - pos[0]))
        out[:k] = tone[pos[0]:pos[0] + k, None]
        pos[0] += k
        return out

    return backend.open_output(dev["index"], rate, int(dev["channels"]), pull)


TONE_TOTAL_S = TONE_LEAD_S + 0.5 + 0.4


class SpeakerOut:
    """Delay line + drift-correcting resampler feeding one output device."""

    CAP_S = 4.0
    JUMP_S = 0.04       # further than this from the target delay ...
    JUMP_HOLD_S = 0.5   # ... for this long: jump instead of resampling. Bluetooth links
                        # shared with another device (a phone connected to the same
                        # speaker) pause and then catch up in a burst, and that
                        # sorts itself out if left alone.

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
        self.t_primed = 0.0   # when it last started playing real audio
        self.err = 0.0
        self.target_s = 0.1
        self.resync = False   # set when the delay is changed: jump to it straight away
        self.t_ok = time.perf_counter()   # last time the delay was close to the target
        self.volume = 1.0
        self.mute = False
        self.peak = 0.0
        self.underruns = 0
        self.beep = None
        self.t_pull = time.perf_counter()   # last time the device asked for audio
        self.lock = threading.Lock()

    def reset(self):
        """Forget the playback position; the next reads line up with the
        others again (used when the speaker rejoins)."""
        with self.lock:
            self.primed = False
            self.r, self.frac, self.err = self.w, 0.0, 0.0

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
            now = time.perf_counter()
            # Smooth estimate of how much audio has "arrived" by now.
            age = min(max(now - self.t_w, 0.0), 0.03)
            w_est = self.w + age * rate
            floor = (self.base_ratio * 1.01 * n_out + 3) * 1.5
            target = max(self.target_s * rate, floor)
            fill = w_est - (self.r + self.frac)
            if not self.primed:
                if fill < target:
                    return silence
                self.r, self.frac, self.err = int(w_est - target), 0.0, 0.0
                self.primed, self.resync, self.t_ok = True, False, now
                self.t_primed = now
                fill = target
            err = fill - target
            if abs(err) <= self.JUMP_S * rate:
                self.t_ok = now
            elif (self.resync or now - self.t_ok > self.JUMP_HOLD_S
                  or self.r < self.w - self.cap + floor):   # about to be overwritten
                self.r = max(int(w_est - target), self.w - self.cap + int(floor), 0)
                self.frac, self.err, err = 0.0, 0.0, 0.0
                self.resync, self.t_ok = False, now
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
        self.t_pull = time.perf_counter()
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
    # A speaker that asks for no audio for this long has dropped out. Shorter
    # pauses happen when the speaker's radio is busy with another connected
    # device; the speaker catches up by itself, so don't restart everyone for it.
    STALL_S = 4.0
    # A rejoining speaker gets its surround channels back only once it has been
    # taking audio steadily for this long (Bluetooth speakers are often silent
    # for a moment after waking up) ...
    WARM_S = 0.5
    # ... and surround channels always move between speakers with a fade this long.
    FADE_S = 0.5

    def __init__(self, backend):
        self.backend = backend
        self.running = False
        self.outs: list[SpeakerOut] = []
        self.plan = None
        self.click_on = False
        self._out_streams = []
        self._capture = None
        self._filters = []
        self._devs = []
        self._settings = None
        self.src_rate = 48000
        self.n_in = 2
        # Speakers being reopened in the background (see rejoin()).
        self._lock = threading.Lock()
        self._gen = 0                 # bumped by stop(): late reopens are discarded
        self._pending = {}            # speaker index -> thread
        self._rejoined = []           # (speaker index, stream or None)
        self._warming = set()         # rejoined speakers not yet given their surround channels
        self._fade = None             # [matrix faded from, matrix faded to, progress 0..1]

    # ---- lifecycle ---------------------------------------------------
    def start(self, source: dict, outputs: list, settings: MixSettings):
        """Start playing. A speaker that cannot be opened is skipped (its
        index is returned) so the others still play; raises only if no
        speaker or the source cannot be opened."""
        self.stop()
        self.src_rate = int(source["rate"])
        self.n_in = int(source["channels"])
        self.outs = [SpeakerOut(o["name"], self.src_rate, int(o["rate"])) for o in outputs]
        self._dev_rates = [int(o["rate"]) for o in outputs]
        self._devs = list(outputs)
        self._filters = [None] * len(self.outs)
        self.apply(settings)
        self.running = True
        failed, first_error = [], None
        for i, (o, dev) in enumerate(zip(self.outs, outputs)):
            try:
                self._out_streams.append(self.backend.open_output(
                    dev["index"], int(dev["rate"]), int(dev["channels"]), o.pull))
            except Exception as e:
                self._out_streams.append(None)
                failed.append(i)
                first_error = first_error or e
        if outputs and len(failed) == len(outputs):
            self.stop()
            raise first_error
        if failed:
            self.apply(settings)        # pan surround over the speakers that did open
        try:
            self._capture = self.backend.open_capture(
                source["index"], self.src_rate, self.n_in, self._on_capture)
        except Exception:
            self.stop()
            raise
        grace = time.perf_counter() + 2.0      # some speakers take a moment to start
        for o in self.outs:
            o.t_pull = grace
        threading.Thread(target=self._click_loop, daemon=True).start()
        return failed

    def stop(self):
        self.running = False
        with self._lock:
            self._gen += 1
            late, self._rejoined = [s for _, s in self._rejoined], []
        for t in self._pending.values():    # the device re-scan after a stop needs them finished
            t.join(timeout=5.0)
        self._pending = {}
        self._warming = set()
        for s in self._out_streams + [self._capture] + late:
            self._close(s)
        self._out_streams = []
        self._capture = None
        self.outs = []
        self.plan = None
        self._fade = None

    def _close(self, stream):
        if stream is not None:
            try:
                self.backend.close(stream)
            except Exception:
                pass

    # ---- dropout detection --------------------------------------------
    def stalled(self):
        """Indices of speakers whose device has stopped asking for audio
        (switched off, out of range, Bluetooth link lost)."""
        now = time.perf_counter()
        return [i for i, (o, s) in enumerate(zip(self.outs, self._out_streams))
                if s is not None and now - o.t_pull > self.STALL_S]

    def drop(self, i):
        """Close one speaker's stream; the other speakers keep playing (in
        surround, its channels are re-panned onto them)."""
        if 0 <= i < len(self._out_streams) and self._out_streams[i] is not None:
            s, self._out_streams[i] = self._out_streams[i], None
            self._warming.discard(i)
            self._close(s)
            self._reapply()

    def index_of(self, name):
        return next((i for i, o in enumerate(self.outs) if o.name == name), None)

    def is_open(self, i):
        return 0 <= i < len(self._out_streams) and self._out_streams[i] is not None

    def is_playing(self, i):
        """True once a speaker is open, taking audio and past its warm-up."""
        return self.is_open(i) and self.outs[i].primed and i not in self._warming

    def rejoin(self, i):
        """Reopen a dropped speaker in the background while the others keep
        playing. It comes back time-aligned with them. The result arrives
        through poll_rejoined(). Returns False if there is nothing to do."""
        if i in self._pending:
            return True                 # already on its way back
        if not self.running or self.is_open(i) or not 0 <= i < len(self.outs):
            return False
        o, dev, gen = self.outs[i], self._devs[i], self._gen
        o.reset()

        def run():
            try:
                s = self.backend.open_output(dev["index"], int(dev["rate"]), int(dev["channels"]), o.pull)
            except Exception:
                s = None
            with self._lock:
                if gen == self._gen:
                    self._rejoined.append((i, s))
                    return
            self._close(s)              # playback was stopped meanwhile

        t = threading.Thread(target=run, name="rejoin", daemon=True)
        self._pending[i] = t
        t.start()
        return True

    def poll_rejoined(self):
        """Put speakers reopened by rejoin() back into playback, and give
        warmed-up ones their surround channels back. Returns [(index, ok)].
        Call regularly, from the same thread that calls apply()."""
        with self._lock:
            done, self._rejoined = self._rejoined, []
        for i, s in done:
            self._pending.pop(i, None)
            if s is not None:
                self.outs[i].t_pull = time.perf_counter() + 2.0   # let it start up
                self._out_streams[i] = s
                self._warming.add(i)
        now = time.perf_counter()
        warm = {i for i in self._warming
                if self.outs[i].primed and now - self.outs[i].t_primed >= self.WARM_S}
        if warm:
            self._warming -= warm
            self._reapply()
        return [(i, s is not None) for i, s in done]

    def _reapply(self):
        if self._settings is not None:
            self.apply(self._settings)

    # ---- configuration (safe to call while running) -------------------
    def apply(self, st: MixSettings):
        n = len(self.outs)
        if n == 0 or len(st.speakers) != n:
            return
        self._settings = st
        trims = np.array([s.trim_ms / 1000.0 for s in st.speakers])
        comp = np.zeros(n)
        M = None
        if st.mode == "surround":
            pos = [s.pos for s in st.speakers]
            az, U = dsp.bed(self.n_in, st.upmix)
            # Pan only onto speakers that are playing, so a dropped speaker's
            # channels aren't lost. Delay and level compensation still use every
            # speaker, so the others' timing doesn't move when one drops out.
            live = [i for i in range(n) if self.is_open(i) and i not in self._warming] or list(range(n))
            G = np.zeros((n, len(az)), np.float32)
            G[live] = dsp.pan_gains([pos[i] for i in live], st.listener, az, st.focus)
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
            target = min(st.base_ms / 1000.0 + float(t), SpeakerOut.CAP_S - 1.0)
            if abs(target - o.target_s) > 0.001:
                o.resync = True
            o.target_s = target
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
            mono = self._faded_mix(block, M)
            for i, o in enumerate(outs):
                o.write(np.repeat(mono[:, i:i + 1], 2, axis=1))
        else:
            self._fade = None           # entering surround later starts without a fade
            stereo = block @ D.T
            for i, o in enumerate(outs):
                f = self._filters[i] if mode == "frequency" else None
                o.write(f.process(stereo) if f is not None else stereo)

    def _faded_mix(self, block, M):
        """block @ M.T, but when M changes (a speaker dropped out or came back,
        or the layout changed) glide from the old matrix to the new one over
        FADE_S instead of switching at once."""
        f = self._fade
        if f is None or f[0].shape != M.shape:
            f = self._fade = [M, M, 1.0]
        elif f[1] is not M:             # new target: start from wherever the last fade got to
            cur = f[0] + (f[1] - f[0]) * np.float32(f[2])
            f = self._fade = [cur, M, 0.0]
        if f[2] >= 1.0:
            return block @ M.T
        n = len(block)
        p1 = min(1.0, f[2] + n / (self.FADE_S * self.src_rate))
        a = np.linspace(f[2], p1, n, endpoint=False, dtype=np.float32)[:, None]
        f[2] = p1
        return (block @ f[0].T) * (1.0 - a) + (block @ M.T) * a

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
    def beep(self, name):
        """Play the identify tone on the speaker with this name. Returns False if
        that speaker isn't playing right now (not part of playback, or dropped out)."""
        for o, s, rate in zip(self.outs, self._out_streams, self._dev_rates):
            if o.name == name and s is not None:
                o.beep = beep_tone(rate)
                return True
        return False

    def levels(self):
        lv = []
        for o in self.outs:
            lv.append(o.peak)
            o.peak = 0.0
        return lv

    def underruns(self):
        return sum(o.underruns for o in self.outs)
