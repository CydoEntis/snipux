import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
sys.path.insert(0, ".")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QRect
from PyQt6.QtTest import QTest
from PyQt6.QtCore import Qt
from snipux.overlay import OverlayWindow
from snipux import design
from tests.test_overlay import make_frame

app = QApplication.instance() or QApplication([])

frame = make_frame(image_size=(1600, 1000), logical_size=(1600, 1000))
overlay = OverlayWindow(frame)
overlay.show()
QTest.qWaitForWindowExposed(overlay)
overlay.set_selection(QRect(400, 200, 200, 150))

bar_width_before = overlay._bar.width()

QTest.mouseClick(overlay._bar._chip, Qt.MouseButton.LeftButton)
tokens = design.tokens
target_label = tokens.CAPTURE_MODES[2][0]  # Full screen
print("target label:", target_label)
QTest.mouseClick(overlay._popover._rows[target_label], Qt.MouseButton.LeftButton)

overlay._bar.grab()  # force a real layout/paint pass

label = overlay._bar._chip._text_label
granted = label.geometry().width()
hint = label.sizeHint().width()
print("bar width before:", bar_width_before, "after:", overlay._bar.width())
print("chip text:", label.text())
print("granted:", granted, "hint:", hint)
print("chip geometry:", overlay._bar._chip.geometry())
print("bar geometry:", overlay._bar.geometry())
