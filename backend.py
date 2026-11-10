"""Windows audio I/O through WASAPI (PyAudioWPatch): loopback capture of what
the PC is playing, plus one output stream per speaker."""
from __future__ import annotations

import numpy as np


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

        def cb(in_data, frame_count, time_info, status):
            try:
                callback(np.frombuffer(in_data, np.float32).reshape(-1, channels))
            except Exception:
                pass
            return (None, pa.paContinue)

        return self.p.open(format=pa.paFloat32, channels=channels, rate=rate, input=True,
                           input_device_index=index, frames_per_buffer=max(128, rate // 100),
                           stream_callback=cb)

    def open_output(self, index, rate, channels, pull):
        pa = self.pa

        def cb(in_data, frame_count, time_info, status):
            try:
                data = pull(frame_count)
                if channels == 1:
                    data = data.mean(axis=1, keepdims=True)
            except Exception:
                data = np.zeros((frame_count, channels), np.float32)
            return (np.ascontiguousarray(data, np.float32).tobytes(), pa.paContinue)

        return self.p.open(format=pa.paFloat32, channels=channels, rate=rate, output=True,
                           output_device_index=index, frames_per_buffer=max(128, rate // 100),
                           stream_callback=cb)

    def close(self, stream):
        try:
            stream.stop_stream()
        finally:
            stream.close()
