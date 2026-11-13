"""Speakers dropping out and coming back.

Link is RoomCast's own record of one speaker's connection (and Drop the part
that exists while it is out of playback). DropoutHandling is the part of the
main window that keeps them up to date: it notices a speaker leaving, puts it
back into playback when it returns without interrupting the others, and
handles Reconnect / Disconnect. It is a mixin of MainWindow and uses the
window's engine, endpoints, monitor, pause guard and speaker rows."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QTimer

from diag import log


@dataclass
class Drop:
    """A speaker taken out of playback while the others keep playing."""
    t: float = field(default_factory=time.monotonic)   # when it was taken out
    gone: bool = False          # disconnected: wait for Windows to report it back
    hold: float = 10.0          # not disconnected: try it again after this many seconds
    rejoining: bool = False     # being reopened
    switched_off: bool = False  # the pause guard found it switched off ...
    await_off: float = -1e9     # ... at this time, before Windows noticed


@dataclass
class Link:
    """RoomCast's own record of one speaker's connection. What Windows
    reports about it is in MainWindow.endpoints."""
    drop: Drop | None = None            # out of playback until it comes back, or None
    drop_times: list = field(default_factory=list)   # recent drops, for the back-off
    rejoin_fails: int = 0               # failed in-place rejoins in a row
    last_rescan: float = -1e9           # last full restart made to bring it back
    settling: bool = False              # back from a dropout: silent until its own reset ...
    steady_since: float | None = None   # ... SETTLE_S after it started playing steadily
    resetting: bool = False             # closed for a moment by _reset_speaker()
    reconnecting: float | None = None   # when Reconnect was pressed, until it connects or fails
    reconnect_failed: float = -1e9      # when a reconnect last failed
    disconnecting: float | None = None  # when Disconnect was pressed
    user_disconnected: bool = False     # disconnected on purpose: don't auto-reconnect it
    last_auto: float = -1e9             # last automatic reconnect request

    def back_off(self, now):
        """Seconds to leave it out after another drop: longer each time it
        keeps dropping out (10 s, 20 s, … up to 2 min)."""
        self.drop_times = [t for t in self.drop_times if now - t < 300] + [now]
        return min(10 * 2 ** (len(self.drop_times) - 1), 120)



class DropoutHandling:
    SETTLE_S = 3.5          # a returned speaker stays connected (silent) this long before its own reset ...
    RESET_GAP_MS = 2000     # ... which closes it for this long

    def _reset_speaker(self, name):
        """Close one speaker for a moment and bring it back, while the others
        keep playing (in Surround its channels fade over to them meanwhile).
        Used once on a speaker that came back after a dropout: its Bluetooth
        link was unsteady right after reconnecting and the speaker may have
        grown its own buffer, leaving it late. A fresh start on a now-steady
        link clears that."""
        i = self.engine.index_of(name)
        if i is None or not self.engine.is_open(i) or self._resyncing:
            return
        log.info("%s: resetting this speaker", name)
        self.engine.drop(i)
        link = self._link(name)
        link.resetting = True
        gen = self.engine._gen
        self._update_rows()

        def back():
            if not link.resetting:
                return
            j = self.engine.index_of(name)
            if (not self.engine.running or self.engine._gen != gen or j is None
                    or link.drop or not self.engine.rejoin(j)):
                link.resetting = False          # stopped, restarted or gone meanwhile
                self._update_rows()
        QTimer.singleShot(self.RESET_GAP_MS, back)

    def _speaker_switched_off(self, name):
        """The pause guard found that this speaker has been switched off,
        though Windows will only notice in a while. Take it out of playback
        now (its channels move to the others); it rejoins once Windows has
        seen it go and come back."""
        link = self._link(name)
        if link.drop or not self.start_btn.isChecked():
            return
        i = self.engine.index_of(name)
        if i is not None and self.engine.is_open(i):
            self._drop_now(name, gone=True, why="switched off: stopped answering after its pause")
        elif self._resyncing:               # mid-resync: don't reopen it when playback restarts
            log.info("%s: switched off during a resync, left out", name)
            self.dropout_count += 1
            self._mark_dropped(name, gone=True)
        else:
            return
        link.drop.switched_off, link.drop.await_off = True, time.monotonic()
        link.settling, link.steady_since = False, None
        self._update_rows()

    def _guard_speakers(self):
        # Tell the pause guard which Bluetooth speakers are playing now.
        res = {}
        if self.engine.running:
            for i, o in enumerate(self.engine.outs):
                e = self.endpoints.get(o.name)
                if (e and e.get("address") and e["connected"] and self.engine.is_open(i)
                        and not self._dropped(o.name)):
                    res[o.name] = e["address"]
        self.guard.set_speakers(res)

    # ---- dropouts and reconnecting --------------------------------------
    def _link(self, name):
        # The speaker's connection record, made on first use.
        link = self.links.get(name)
        if link is None:
            link = self.links[name] = Link()
        return link

    def _dropped(self, name):
        # The speaker's Drop while it is out of playback, else None.
        link = self.links.get(name)
        return link.drop if link else None

    def _drop_record(self, name):
        # The speaker's Drop, made (with a 10 s retry) if it isn't out of playback yet.
        link = self._link(name)
        if link.drop is None:
            link.drop = Drop()
        return link.drop

    def _mark_dropped(self, name, gone=False, hold=None):
        """Leave a speaker out of playback until it comes back, or retry it after
        `hold` s (by default longer each time it keeps dropping out)."""
        link, now = self._link(name), time.monotonic()
        link.drop = Drop(now, gone, link.back_off(now) if hold is None else hold)

    def _drop_now(self, name, gone=False, why=None):
        # Take one speaker out of playback; the others keep playing.
        log.info("%s: dropped (%s)", name, why or ("Windows says disconnected" if gone
                                                   else "stopped taking audio"))
        link = self._link(name)
        if not link.drop and not link.user_disconnected:
            self.dropout_count += 1             # one outage, however many ways it is noticed
        i = self.engine.index_of(name)
        if i is not None:
            self.engine.drop(i)                 # surround re-pans onto the others by itself
        self._mark_dropped(name, gone)

    def _schedule_rejoin(self, name, ms):
        d = self._drop_record(name)
        d.gone, d.rejoining = False, True
        QTimer.singleShot(ms, lambda: self._try_rejoin(name))

    def _try_rejoin(self, name):
        """Put a returning speaker back into playback without interrupting the
        others. Falls back to restarting playback when that isn't possible
        (e.g. it wasn't part of this playback session)."""
        link = self._link(name)
        if not self.start_btn.isChecked() or self._resyncing:
            link.drop = None                    # a resync under way starts it with the others
            return
        e = self.endpoints.get(name)
        if e is not None and not e["connected"]:    # still off: wait until Windows sees it again
            d = self._drop_record(name)
            d.gone, d.rejoining = True, False
            self._update_rows()
            return
        i = self.engine.index_of(name) if self.engine.running else None
        if i is not None:
            self.engine.drop(i)                 # a stream left over from before it disconnected
            if self.engine.rejoin(i):
                log.info("%s: reopening", name)
                self._drop_record(name).rejoining = True
                self._update_rows()
                return
        log.info("%s: can't rejoin in place, restarting playback", name)
        link.drop = None
        self._schedule_restart(500)

    def _log_pull_stats(self):
        """Note in the log when a speaker asks for audio unevenly: the only
        sign of trouble before Windows notices a lost Bluetooth link."""
        now = time.monotonic()
        dt = now - getattr(self, "_last_stats", now)
        if dt < 1.0:
            return
        self._last_stats = now
        quiet = now - getattr(self, "_last_full_stats", -1e9) < 30   # every 30 s, log everything
        if not quiet:
            self._last_full_stats = now
        for name, gap, frames, rate in self.engine.pull_stats():
            speed = frames / (rate * dt)
            if not quiet or gap > 0.12 or abs(speed - 1.0) > 0.15:
                log.info("%s: longest pause %.0f ms, took audio at %.0f%% speed",
                         name, gap * 1000, speed * 100)

    def _check_dropouts(self):
        now = time.monotonic()
        if self.engine.running:
            self._log_pull_stats()
            stalled = self.engine.stalled()
            for i in stalled:
                self._drop_now(self.engine.outs[i].name)
            changed = bool(stalled)
            for i, ok in self.engine.poll_rejoined():
                changed = True
                name = self.engine.outs[i].name
                link = self._link(name)
                was_reset, link.resetting = link.resetting, False
                log.info("%s: reopen %s%s", name, "ok" if ok else "failed",
                         " (after its reset)" if was_reset else "")
                if ok:
                    link.drop, link.rejoin_fails = None, 0
                    # Back from a dropout: keep it silent until it has settled and had
                    # its own reset, so it doesn't play late and then cut out again.
                    # Back from that reset: let it play.
                    if not was_reset:
                        link.settling, link.steady_since = True, None
                    self.engine.hold(i, not was_reset)
                    continue
                link.rejoin_fails += 1
                gone = bool(link.drop and link.drop.gone)
                if link.rejoin_fails == 1:      # try again shortly
                    self._mark_dropped(name, gone, hold=3)
                elif now - link.last_rescan > 300:
                    # Its device entry may be out of date. Re-scanning needs a short
                    # restart of every speaker, so allow it only now and then.
                    link.last_rescan, link.rejoin_fails, link.drop = now, 0, None
                    self._schedule_restart(500)
                else:                           # keep trying now and then, without a restart
                    self._mark_dropped(name, gone)
            if changed:
                self._update_rows()
            # A returned speaker that has played steadily for a while gets one short
            # reset of its own, which clears the extra lag it picked up while its
            # link was still unsteady. The others keep playing.
            for name, link in list(self.links.items()):
                if not link.settling:
                    continue
                i = self.engine.index_of(name)
                if i is None or not self.engine.is_open(i):
                    link.settling, link.steady_since = False, None     # dropped again
                    self._update_rows()
                elif not self.engine.is_playing(i):
                    link.steady_since = None
                elif link.steady_since is None:
                    link.steady_since = now
                elif now - link.steady_since >= self.SETTLE_S:
                    link.settling, link.steady_since = False, None
                    self._reset_speaker(name)
        for name, link in list(self.links.items()):
            d = link.drop
            if d and not d.gone and not d.rejoining and now - d.t > d.hold:
                self._try_rejoin(name)          # never actually disconnected: try it again

    def _on_monitor(self, endpoints, battery):
        old = self.endpoints
        self.endpoints, self.battery = self._index(endpoints), battery
        now = time.monotonic()

        def shape(d):
            return {(n, e["connected"], e["hands_free"]) for n, e in d.items()}

        def up(d):
            return {n for n, e in d.items() if e["connected"] and not e["hands_free"]}

        returned = set()
        for name, link in self.links.items():
            e = self.endpoints.get(name)
            connected = e is not None and e["connected"]
            d = link.drop
            if d and e is not None:             # (no connection info: the time-out retries it)
                if not connected:
                    d.gone, d.await_off = True, -1e9
                elif d.gone and now - d.await_off >= 40:    # (before that: switched off,
                    returned.add(name)                      # but Windows hasn't noticed yet)
            if link.disconnecting is not None and (not connected or now - link.disconnecting > 15):
                link.disconnecting = None
            if link.user_disconnected and connected and link.disconnecting is None:
                link.user_disconnected = False  # connected again some other way
            if link.reconnecting is not None:
                if connected:
                    link.reconnecting = None
                elif now - link.reconnecting > 20:
                    link.reconnecting, link.reconnect_failed = None, now

        if self.start_btn.isChecked():
            # Speakers leaving or returning are handled one at a time so the
            # others keep playing. Anything else (another speaker appearing, a
            # hands-free endpoint coming and going because a phone connected to
            # a speaker) doesn't touch playback at all.
            checked = set(self.checked)
            came_up = ((up(self.endpoints) - up(old)) & checked) | returned
            for name in (up(old) - up(self.endpoints)) & checked:
                d = self._dropped(name)
                if d and d.switched_off:
                    continue                        # already taken out when it was switched off
                self._drop_now(name, gone=True)
            for name in came_up:
                i = self.engine.index_of(name)
                if name not in returned and i is not None and self.engine.is_open(i):
                    continue                        # already playing (e.g. first poll after launch)
                log.info("%s: back, rejoining in 2 s", name)
                self._schedule_rejoin(name, 2000)   # let a fresh Bluetooth link settle first
            if shape(old) != shape(self.endpoints):
                self._add_new_rows()                # ticking one restarts playback to open it
        elif shape(old) != shape(self.endpoints):
            self.refresh_devices()

        if self.start_btn.isChecked() and self.opts["auto_reconnect"]:
            for name in self.checked:
                e, link = self.endpoints.get(name), self._link(name)
                if (e and e["bluetooth"] and not e["connected"] and link.reconnecting is None
                        and not link.user_disconnected and now - link.last_auto > 30):
                    link.last_auto = now
                    self.monitor.request_reconnect(e["id"], name)
        self._keep_source_default()
        self._update_rows()

    def _reconnect(self, name):
        e = self.endpoints.get(name)
        if e is None:
            return
        link = self._link(name)
        link.reconnecting, link.reconnect_failed = time.monotonic(), -1e9
        link.user_disconnected = False
        self.monitor.request_reconnect(e["id"], name)
        self._update_rows()

    def _disconnect(self, name):
        """Disconnect a Bluetooth speaker from the PC (it stays paired). If it is
        playing, the other speakers carry on without it."""
        e = self.endpoints.get(name)
        if e is None or not e["connected"]:
            return
        link = self._link(name)
        link.disconnecting, link.user_disconnected = time.monotonic(), True
        self.monitor.request_disconnect(e["id"], name)
        self._update_rows()

    def _on_disconnect_done(self, name, ok, message):
        if not ok:
            link = self._link(name)
            link.disconnecting, link.user_disconnected = None, False
            self._flash("Couldn't disconnect %s%s. You can disconnect it in Windows Bluetooth "
                        "settings." % (name, ": " + message if message else ""), 10)
            self._update_rows()

    def _on_reconnect_done(self, name, ok, message):
        link = self._link(name)
        if not ok and link.reconnecting is not None:
            link.reconnecting, link.reconnect_failed = None, time.monotonic()
            self._update_rows()
        if ok:
            self.monitor.poll_now()

    def _switched_off(self, name):
        # Turned off (found by the pause guard) but Windows doesn't know yet.
        e, d = self.endpoints.get(name), self._dropped(name)
        return bool(e is not None and e["connected"] and d and d.switched_off)
