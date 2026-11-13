import ctypes
import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

import diag
from ui import MainWindow
from widgets import apply_theme


def main():
    diag.setup()
    diag.log.info("RoomCast started")
    # Own taskbar identity, so Windows shows RoomCast's icon instead of Python's.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RoomCast")
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "assets", "roomcast-icon.svg")))
    apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
