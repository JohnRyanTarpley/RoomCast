"""Keep music playing when a Bluetooth speaker is switched off.

Many Bluetooth speakers send "pause" to the PC as they switch off. Windows
hands that straight to the music app (it never appears as a key press, so it
can't be intercepted), and the app pauses for every speaker. Windows itself
only notices the speaker has gone some 20 s later, when its link times out.

So whenever an app pauses while RoomCast is playing, the guard asks the
speakers a live question over Bluetooth (an uncached service query). After a
button press every speaker answers in well under a second. A speaker that is
switching off stops answering within a second or two of its pause, and while
Windows keeps trying to reach it, every other speaker's query hangs too. So a
query that doesn't answer means "a speaker has just been switched off", but
on its own not which one. Speakers are asked one at a time: when one answers
and the next one asked hangs moments later, the one that hung is the one
that went, and it is reported (RoomCast takes it out of playback at once).
When the very first query hangs there is nothing to compare with, so no
speaker is named and taking it out is left to the normal handling once
Windows notices. Either way the paused apps are told to play again. If every
speaker answers, the pause was meant and is left alone.

Needs the optional winrt Media.Control and Bluetooth packages; without them
the guard does nothing."""
from __future__ import annotations

import asyncio
import threading
import time

from PySide6.QtCore import QObject, Signal

from diag import log


class PauseGuard(QObject):
    paused = Signal()                   # the music paused (no app is playing any more)
    checking = Signal(bool)             # a check of the speakers started (True) / ended (False)
    switched_off = Signal(str)          # ... and this is the one (when it could be told)

    FIRST_CHECK_S = 1.0     # a speaker can still answer just after sending its pause
    SECOND_CHECK_S = 2.0    # ... so ask again a little later before believing it
    ANSWER_S = 1.6          # a speaker that is on answers in ~0.1-1.1 s
    RESUME_WITHIN_S = 12.0  # only undo a pause this recent

    def __init__(self):
        super().__init__()
        self.enabled = True
        self._speakers = {}             # name -> Bluetooth address, of the speakers playing now
        self._lock = threading.Lock()
        self._busy = False              # a check is running
        self._hung = 0                  # queries that didn't answer in time and are still running
        self.available = self._check_available()
        self._loop = None               # the guard thread's event loop, once running
        self._mgr = None                # media session manager, once running
        self._ours = {}                 # app id -> when RoomCast itself paused it (not a speaker)
        self._to_resume = []            # sessions pause_all(remember=True) paused, for resume()

    @staticmethod
    def _check_available():
        try:
            import winrt.windows.devices.bluetooth  # noqa: F401
            import winrt.windows.devices.bluetooth.rfcomm  # noqa: F401
            import winrt.windows.media.control  # noqa: F401
            return True
        except Exception as e:
            log.info("pause guard unavailable: %s", e)
            return False

    def set_speakers(self, speakers):
        """{name: Bluetooth address} of the Bluetooth speakers playing right now
        (empty while stopped). Safe to call from any thread."""
        with self._lock:
            self._speakers = dict(speakers)

    def start(self):
        if self.available:
            threading.Thread(target=lambda: asyncio.run(self._run()), name="pause-guard",
                             daemon=True).start()

    # ---- background thread ---------------------------------------------
    async def _run(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
        try:
            mgr = await Manager.request_async()
        except Exception as e:
            log.info("pause guard: media sessions unavailable: %s", e)
            return
        self._loop, self._mgr = asyncio.get_running_loop(), mgr
        playing = {}                    # app id -> was playing at the last look
        paused_at = {}                  # app id -> (time it paused, session)
        while True:
            await asyncio.sleep(0.15)
            try:
                sessions = list(mgr.get_sessions())
            except Exception:
                continue
            now = time.monotonic()
            fresh = []
            for s in sessions:
                try:
                    app = s.source_app_user_model_id
                    st = s.get_playback_info().playback_status
                except Exception:
                    continue
                is_playing = st == Status.PLAYING
                if playing.get(app) and st == Status.PAUSED:
                    if now - self._ours.pop(app, -1e9) < 5.0:
                        playing[app] = False    # RoomCast paused it itself: nothing to check
                        continue
                    paused_at[app] = (now, s)
                    fresh.append(app)
                elif is_playing:
                    paused_at.pop(app, None)
                playing[app] = is_playing
            for app in [a for a, (t, _) in paused_at.items() if now - t > self.RESUME_WITHIN_S]:
                del paused_at[app]
            if fresh and not any(playing.values()):
                self.paused.emit()
            if fresh and self.enabled and not self._busy and not self._hung:
                with self._lock:
                    speakers = dict(self._speakers)
                if speakers:
                    log.info("pause guard: %s paused, checking the speakers", ", ".join(fresh))
                    self._busy = True
                    asyncio.ensure_future(self._check(speakers, paused_at))

    def pause_all(self, remember=False):
        """Pause every app that is playing media (when RoomCast is stopped, or
        around a Resync). remember=True keeps them for resume(). Safe to call
        from any thread; does nothing if unavailable."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._pause_all(remember), self._loop)

    def resume(self):
        """Play again the apps the last pause_all(remember=True) paused."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._resume(), self._loop)

    async def _pause_all(self, remember):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
        try:
            sessions = list(self._mgr.get_sessions())
        except Exception:
            return
        self._to_resume = []
        for s in sessions:
            try:
                if s.get_playback_info().playback_status == Status.PLAYING:
                    app = s.source_app_user_model_id
                    self._ours[app] = time.monotonic()
                    await s.try_pause_async()
                    if remember:
                        self._to_resume.append(s)
                    log.info("paused %s for RoomCast", app)
            except Exception as e:
                log.info("couldn't pause a media app: %s", e)

    async def _resume(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
        sessions, self._to_resume = self._to_resume, []
        for s in sessions:
            try:
                if s.get_playback_info().playback_status == Status.PAUSED:   # not stopped meanwhile
                    await s.try_play_async()
                    log.info("resumed %s after the resync", s.source_app_user_model_id)
            except Exception as e:
                log.info("couldn't resume a media app: %s", e)

    async def _check(self, speakers, paused_at):
        self.checking.emit(True)
        try:
            t0 = time.monotonic()
            stuck, gone, last_ok = False, None, {}
            for delay in (self.FIRST_CHECK_S, self.SECOND_CHECK_S):
                await asyncio.sleep(max(0.0, t0 + delay - time.monotonic()))
                if not paused_at:
                    return                          # someone pressed play meanwhile
                for name, addr in speakers.items():
                    if await self._answers(name, addr):
                        last_ok[name] = time.monotonic()
                        continue
                    stuck = True                    # the others' queries would hang too
                    now = time.monotonic()
                    if any(n != name and now - t < self.ANSWER_S + 0.5 for n, t in last_ok.items()):
                        gone = name                 # another answered just before: it's this one
                    break
                if stuck:
                    break
            if not stuck:
                log.info("pause guard: every speaker answered, so the pause was meant")
                return
            if gone:
                log.info("pause guard: %s stopped answering right after another answered: "
                         "switched off (%.1f s after the pause)", gone, time.monotonic() - t0)
                self.switched_off.emit(gone)
            else:
                log.info("pause guard: Bluetooth stopped answering: a speaker was switched off, "
                         "can't tell which yet")
            if len(speakers) < 2:
                return                              # nothing left to play on
            if gone:
                asyncio.ensure_future(self._watch_recovery(
                    {n: a for n, a in speakers.items() if n != gone}, t0))
            for app, (t, s) in list(paused_at.items()):
                try:
                    ok = await s.try_play_async()
                    log.info("pause guard: resumed %s (%s)", app, "ok" if ok else "refused")
                except Exception as e:
                    log.info("pause guard: couldn't resume %s: %s", app, e)
            paused_at.clear()
        finally:
            self._busy = False
            self.checking.emit(False)

    async def _watch_recovery(self, speakers, t0):
        """For the log: after the switched-off speaker was taken out, how soon
        do the others answer again (i.e. does Bluetooth recover before Windows
        gives up on the lost speaker)?"""
        for _ in range(20):
            await asyncio.sleep(1.0)
            for name, addr in speakers.items():
                if await self._answers(name, addr, quiet=True):
                    log.info("pause guard: %s answers again, %.1f s after the pause",
                             name, time.monotonic() - t0)
                    return
        log.info("pause guard: the other speakers still weren't answering 20 s later")

    async def _answers(self, name, addr, quiet=False):
        from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothDevice
        t = time.monotonic()

        async def query():
            dev = await BluetoothDevice.from_bluetooth_address_async(addr)
            if dev is None:
                raise OSError("not found")
            try:
                await dev.get_rfcomm_services_async(BluetoothCacheMode.UNCACHED)
            finally:
                try:
                    dev.close()
                except Exception:
                    pass

        task = asyncio.ensure_future(query())
        try:
            await asyncio.wait_for(asyncio.shield(task), self.ANSWER_S)
            if not quiet:
                log.info("pause guard: %s answered in %.0f ms", name, (time.monotonic() - t) * 1000)
            return True
        except asyncio.TimeoutError:
            # Leave the hung query running (cancelling it doesn't stop Windows),
            # and don't start another check until it has finished.
            self._hung += 1

            def ended(_t):
                self._hung -= 1
                ok = not _t.cancelled() and _t.exception() is None
                log.info("pause guard: %s's query %s after %.0f s", name,
                         "answered" if ok else "failed", time.monotonic() - t)
            task.add_done_callback(ended)
            return False
        except Exception as e:
            log.info("pause guard: %s query failed (%s), counting it as an answer", name, e)
            return True
