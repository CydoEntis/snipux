"""The post-capture review window: what a snip looks like after the overlay
has closed.

Off unless turned on in Settings. The overlay annotates in place and always
has, so this is not a second editor and deliberately carries no drawing
tools -- a second set of tools would be a second implementation to drift
from the first, which is a good part of why the old `editor.py` was deleted
in the overlay redesign. What it adds is the thing the overlay genuinely
cannot: a snip that survives the moment of capture. Copy or Save dismisses
the overlay instantly, so a snip saved to the wrong place, or copied when
you meant to save, previously meant taking the whole capture again.

Everything here operates on the already-flattened `QImage` the overlay hands
over. It never touches `shapes`, `Frame`, or the mark model.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# The preview is a convenience, not the artefact -- a 4K snip must not open a
# 4K window. The image is only ever scaled down, never up, so a small snip
# shows at its true size instead of being blown up and blurred.
_MAX_PREVIEW = (960, 600)


class ReviewWindow(QDialog):
    """Shows `image`, with the ways out the overlay's own Copy/Save can't
    offer once it has closed.

    `saved_path` is where the snip already went, when it was saved rather
    than copied -- it is what makes "Show in folder" meaningful, and its
    absence is why that button is disabled for a copied snip rather than
    hidden: the same window shouldn't change shape depending on how the
    snip got here.
    """

    def __init__(
        self,
        image: QImage,
        *,
        saved_path: Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._image = image
        self._saved_path = saved_path

        self.setWindowTitle(f"snipux — {image.width()} × {image.height()}")
        self.setModal(False)

        layout = QVBoxLayout(self)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setPixmap(self._preview_pixmap())
        layout.addWidget(self._preview)

        self._status = QLabel(
            f"Saved to {self._display_path(saved_path)}"
            if saved_path is not None
            else "Copied to the clipboard — not saved to disk."
        )
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        self._copy_button = QPushButton("Copy")
        self._copy_button.clicked.connect(self.copy)
        buttons.addWidget(self._copy_button)

        self._save_as_button = QPushButton("Save As...")
        self._save_as_button.clicked.connect(self.save_as)
        buttons.addWidget(self._save_as_button)

        self._folder_button = QPushButton("Show in Folder")
        self._folder_button.clicked.connect(self.show_in_folder)
        self._folder_button.setEnabled(saved_path is not None)
        buttons.addWidget(self._folder_button)

        buttons.addStretch()

        self._close_button = QPushButton("Close")
        self._close_button.clicked.connect(self.accept)
        self._close_button.setDefault(True)
        buttons.addWidget(self._close_button)

        layout.addLayout(buttons)

    @staticmethod
    def _display_path(path: Path | None) -> str:
        """`~`-relative where possible: the full path of a screenshot is
        mostly the user's own home directory read back at them.
        """
        if path is None:
            return ""
        try:
            return f"~/{path.relative_to(Path.home())}"
        except ValueError:
            return str(path)

    def _preview_pixmap(self) -> QPixmap:
        pixmap = QPixmap.fromImage(self._image)
        width, height = _MAX_PREVIEW
        if pixmap.width() <= width and pixmap.height() <= height:
            return pixmap
        return pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def copy(self) -> None:
        """Put the snip on the clipboard again.

        Useful even for a snip that was copied on the way here: anything
        else copied since has replaced it, and without this the only way
        back is to re-take the capture.
        """
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setImage(self._image)
        self._status.setText("Copied to the clipboard.")

    def save_as(self, path: Path | str | None = None) -> Path | None:
        """Write the snip somewhere the user picks. Returns the path
        written, or None if they cancelled.

        `path` is only ever passed by tests -- QFileDialog cannot be driven
        from an offscreen platform, and mocking Qt's own static method to
        test our handling of its result tests the mock, not us.
        """
        if path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                "Save snip",
                str(self._suggested_path()),
                "PNG image (*.png)",
            )
            if not chosen:
                return None
            path = chosen
        path = Path(path)
        # An extension-less filename typed into the dialog would otherwise
        # be written as a PNG named without one, which nothing opens by
        # double-click.
        if path.suffix == "":
            path = path.with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._image.save(str(path), "PNG"):
            self._status.setText(f"Could not write {self._display_path(path)}.")
            return None
        self._saved_path = path
        self._folder_button.setEnabled(True)
        self._status.setText(f"Saved to {self._display_path(path)}")
        return path

    def _suggested_path(self) -> Path:
        if self._saved_path is not None:
            return self._saved_path
        return Path.home() / "Pictures" / "snipux"

    def show_in_folder(self) -> None:
        """Open the containing directory in the file manager.

        The directory, not the file: `openUrl` on a PNG opens an image
        viewer, which is not what "show in folder" means anywhere else.
        """
        if self._saved_path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._saved_path.parent)))
