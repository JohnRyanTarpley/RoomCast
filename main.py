import ctypes
import getpass
import os
import re
import sys
import threading
import traceback

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

import diag
from ui import MainWindow
from widgets import apply_theme

# One RoomCast per Windows user: a second copy would fight the first over the speakers.
INSTANCE = "RoomCast-" + re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser())


def install_crash_handlers(app):
    """RoomCast runs without a console, so an unexpected error would vanish.
    Log every distinct one, and tell the user about it (once each)."""
    seen = set()
    showing = [False]

    def report(exc_type, exc, tb, where):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        if text in seen:                # e.g. the same error on every timer tick
            return
        seen.add(text)
        diag.log.error("unexpected error (%s):\n%s", where, text.rstrip())
        if where == "main thread" and not showing[0]:
            showing[0] = True
            try:
                QMessageBox.warning(
                    app.activeWindow(), "RoomCast",
                    "RoomCast ran into an unexpected problem and may not work properly until "
                    "it is restarted:\n\n%s: %s\n\nThe details are in %s."
                    % (exc_type.__name__, exc, diag.LOG_PATH))
            finally:
                showing[0] = False

    def on_main(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        report(exc_type, exc, tb, "main thread")

    def on_thread(args):
        if args.exc_type is SystemExit:
            return
        report(args.exc_type, args.exc_value, args.exc_traceback,
               "thread %s" % (args.thread.name if args.thread else "?"))

    sys.excepthook = on_main             # also called for errors in Qt slots and timers
    threading.excepthook = on_thread


def tell_running_copy():
    """If RoomCast is already running, ask it to show its window. Returns True
    if it did (this copy should then just exit)."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE)
    if not sock.waitForConnected(500):
        return False
    ctypes.windll.user32.AllowSetForegroundWindow(-1)   # let it come to the front (ASFW_ANY)
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def listen_for_copies(app, window):
    """Bring the window (window() returns it, or None before it exists) forward
    whenever another copy is started. Returns None if another copy started at
    the same moment and is running instead."""
    server = QLocalServer(app)

    def show():
        while server.hasPendingConnections():
            server.nextPendingConnection().deleteLater()
        win = window()
        if win is None:
            return
        if win.isMinimized():
            win.showNormal()
        win.show()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(show)
    if not server.listen(INSTANCE):
        if server.serverError() == QAbstractSocket.AddressInUseError and tell_running_copy():
            return None
        diag.log.info("single-instance check unavailable: %s", server.errorString())
    return server


def main():
    diag.setup()
    # Own taskbar identity, so Windows shows RoomCast's icon instead of Python's.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RoomCast")
    app = QApplication(sys.argv)
    if tell_running_copy():
        diag.log.info("RoomCast is already running: showed that window instead")
        return
    windows = []
    server = listen_for_copies(app, lambda: windows[0] if windows else None)
    if server is None:
        diag.log.info("RoomCast is already running: showed that window instead")
        return
    diag.log.info("RoomCast started")
    install_crash_handlers(app)
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "assets", "roomcast-icon.svg")))
    apply_theme(app)
    windows.append(MainWindow())
    windows[0].show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
