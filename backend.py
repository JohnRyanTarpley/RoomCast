"""Windows audio I/O through WASAPI (PyAudioWPatch): loopback capture of what
the PC is playing, plus one output stream per speaker.

Streams normally run in callback mode. Some PCs refuse callback mode on their
real devices ("Unanticipated host error", -9999), which can start happening
while Windows is running, e.g. after another audio program changes the audio
engine's settings. Write/read mode still works then, so each open falls back to
it automatically, driven by a background thread. The rest of RoomCast can't
tell the difference."""
from __future__ import annotations

import threading
import time

import numpy as np


class _Loop:
    """A read/write-mode stream driven by its own thread, with the same
    stop_stream() / close() interface as a callback stream.

    `step` must never block for long (it only reads/writes what is ready), so
    the thread always exits promptly when stopped; the stream is only stopped
    and closed once the thread has finished with it."""

    def __init__(self, stream, step):
        self.stream = stream
        self._step = step
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            try:
                if not self._step(self.stream):
                    time.sleep(0.002)    # nothing ready yet
            except Exception:            # device gone: stop feeding it (RoomCast notices the dropout)
                break

    def _finished(self):
        self._running = False
        self._thread.join(timeout=2.0)
        return not self._thread.is_alive()

    def stop_stream(self):
        if self._finished():
            self.stream.stop_stream()

    def close(self):
        if self._finished():             # never close it under a running read/write
            self.stream.close()


class WasapiBackend:
    def __init__(self):
        import pyaudiowpatch as pa
        self.pa = pa
        self.p = pa.PyAudio()

    def refresh(self):
        """Re-scan devices. All streams must be closed first."""
        self.p.terminate()
        self.p = self.pa.PyAudio()

    def _wasapi(self):
        return self.p.get_host_api_info_by_type(self.pa.paWASAPI)

    def list_outputs(self):
        api = self._wasapi()["index"]
        res = []
        for i in range(self.p.get_device_count()):
            d = self.p.get_device_info_by_index(i)
            if d["hostApi"] == api and d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice"):
                res.append({"index": i, "name": d["name"], "rate": int(d["defaultSampleRate"]),
                            "channels": min(2, int(d["maxOutputChannels"]))})
        return res

    def list_sources(self):
        res = []
        for d in self.p.get_loopback_device_info_generator():
            res.append({"index": d["index"], "name": d["name"], "rate": int(d["defaultSampleRate"]),
                        "channels": int(d["maxInputChannels"])})
        return res

    def default_output_name(self):
        try:
            return self.p.get_device_info_by_index(self._wasapi()["defaultOutputDevice"])["name"]
        except Exception:
            return ""

    def open_capture(self, index, rate, channels, callback):
        pa = self.pa
        frames = max(128, rate // 100)

        def deliver(in_data):
            try:
                callback(np.frombuffer(in_data, np.float32).reshape(-1, channels))
            except Exception:
                pass

        def cb(in_data, frame_count, time_info, status):
            deliver(in_data)
            return (None, pa.paContinue)

        kw = dict(format=pa.paFloat32, channels=channels, rate=rate, input=True,
                  input_device_index=index, frames_per_buffer=frames)
        def step(s):
            if s.get_read_available() < frames:
                return False
            deliver(s.read(frames, exception_on_overflow=False))
            return True

        try:
            return self.p.open(stream_callback=cb, **kw)
        except OSError:
            return _Loop(self.p.open(**kw), step)

    def open_output(self, index, rate, channels, pull):
        pa = self.pa
        frames = max(128, rate // 100)

        def render(frame_count):
            try:
                data = pull(frame_count)
                if channels == 1:
                    data = data.mean(axis=1, keepdims=True)
            except Exception:
                data = np.zeros((frame_count, channels), np.float32)
            return np.ascontiguousarray(data, np.float32).tobytes()

        def cb(in_data, frame_count, time_info, status):
            return (render(frame_count), pa.paContinue)

        kw = dict(format=pa.paFloat32, channels=channels, rate=rate, output=True,
                  output_device_index=index, frames_per_buffer=frames)
        def step(s):
            if s.get_write_available() < frames:
                return False
            s.write(render(frames), exception_on_underflow=False)
            return True

        try:
            return self.p.open(stream_callback=cb, **kw)
        except OSError:
            return _Loop(self.p.open(**kw), step)

    def close(self, stream):
        try:
            stream.stop_stream()
        finally:
            stream.close()
