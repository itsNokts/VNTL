from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QPoint, QSize, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from config import Config, OverlayConfig
from context_manager import DialogueLine
from hooker import HookerService, StreamState, list_processes
from screenshot_service import ScreenshotService


class _ResizeGrip(QWidget):
    """Bottom-right resize handle; works on both Wayland and X11."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._sizing = False
        self._press_pos: QPoint | None = None
        self._press_size: QSize | None = None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(180, 180, 180, 160)
        painter.setPen(color)
        w, h = self.width(), self.height()
        for offset in (4, 8, 12):
            painter.drawLine(w - offset, h, w, h - offset)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._sizing = True
            self._press_pos = event.globalPosition().toPoint()
            self._press_size = self.window().size()

    def mouseMoveEvent(self, event) -> None:
        if self._sizing and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._press_pos
            win = self.window()
            win.resize(
                max(win.minimumWidth(),  self._press_size.width()  + delta.x()),
                max(win.minimumHeight(), self._press_size.height() + delta.y()),
            )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._sizing = False


class _ContextPopup(QFrame):
    """
    Context menu rendered as a top-level Popup window so it can extend
    beyond the overlay's bounds. Qt auto-dismisses it on click-outside.
    """

    _BTN_STYLE = (
        "QPushButton { background: transparent; color: #e0e0e0; border: none;"
        "  text-align: left; padding: 5px 14px; border-radius: 3px; }"
        "QPushButton:hover { background-color: #2e2e3e; }"
    )

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setStyleSheet(
            "QFrame { background-color: #141419; border: 1px solid #3a3a4a;"
            "  border-radius: 4px; }"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)
        self.hide()

    def add_action(self, text: str, callback) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setStyleSheet(self._BTN_STYLE)
        btn.clicked.connect(self.hide)
        btn.clicked.connect(callback)
        self._layout.addWidget(btn)
        return btn

    def add_separator(self) -> None:
        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2a2a3a; max-height: 1px; margin: 2px 4px;")
        self._layout.addWidget(sep)

    def popup_at(self, pos: QPoint) -> None:
        """Show at local `pos`, converted to global screen coordinates."""
        self.adjustSize()
        p = self.parentWidget()
        global_pos = p.mapToGlobal(pos)
        screen = p.screen()
        if screen:
            sg = screen.availableGeometry()
            x = min(global_pos.x(), sg.right()  - self.width())
            y = min(global_pos.y(), sg.bottom() - self.height())
            global_pos = QPoint(max(sg.left(), x), max(sg.top(), y))
        self.move(global_pos)
        self.show()
        self.raise_()


class BacklogWindow(QWidget):
    """
    In-session backlog showing all translated lines (JP + EN) since launch.
    In-memory only — not persisted to disk.
    """

    def __init__(self, ov_cfg: OverlayConfig, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Backlog")
        self.resize(420, 540)
        self._ov_cfg = ov_cfg
        self._entries: list[tuple[QLabel, QLabel]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._inner = QVBoxLayout(self._container)
        self._inner.setContentsMargins(10, 8, 10, 8)
        self._inner.setSpacing(0)
        self._inner.addStretch()
        self._scroll.verticalScrollBar().rangeChanged.connect(
            lambda _min, _max: self._scroll.verticalScrollBar().setValue(_max)
        )
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll)
        self._apply_bg()

    def _apply_bg(self) -> None:
        r, g, b, a = self._ov_cfg.bg_color
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: rgba({r},{g},{b},{a}); border: none; }}"
        )
        self._container.setStyleSheet(f"background: rgba({r},{g},{b},{a});")

    def append_entry(self, jp: str, en: str) -> None:
        if self._entries:
            sep = QFrame(self._container)
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(
                "background-color: rgba(80,80,80,80); max-height: 1px; margin: 4px 0;"
            )
            self._inner.insertWidget(self._inner.count() - 1, sep)

        ov = self._ov_cfg
        _selectable = (
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        jp_lbl = QLabel(jp, self._container)
        r, g, b, a = ov.jp_color
        jp_lbl.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{ov.jp_font_family}'; font-size: {ov.jp_font_size}pt;"
        )
        jp_lbl.setWordWrap(True)
        jp_lbl.setTextInteractionFlags(_selectable)

        en_lbl = QLabel(en, self._container)
        r, g, b, a = ov.en_color
        en_lbl.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{ov.en_font_family}'; font-size: {ov.en_font_size}pt;"
        )
        en_lbl.setWordWrap(True)
        en_lbl.setTextInteractionFlags(_selectable)

        self._inner.insertWidget(self._inner.count() - 1, jp_lbl)
        self._inner.insertWidget(self._inner.count() - 1, en_lbl)
        self._entries.append((jp_lbl, en_lbl))

    def update_last_entry(self, en: str) -> None:
        """Replace the EN text of the most recent backlog entry in-place."""
        if self._entries:
            self._entries[-1][1].setText(en)

    def refresh_appearance(self, ov_cfg: OverlayConfig) -> None:
        self._ov_cfg = ov_cfg
        self._apply_bg()
        for jp_lbl, en_lbl in self._entries:
            r, g, b, a = ov_cfg.jp_color
            jp_lbl.setStyleSheet(
                f"color: rgba({r},{g},{b},{a}); "
                f"font-family: '{ov_cfg.jp_font_family}'; font-size: {ov_cfg.jp_font_size}pt;"
            )
            r, g, b, a = ov_cfg.en_color
            en_lbl.setStyleSheet(
                f"color: rgba({r},{g},{b},{a}); "
                f"font-family: '{ov_cfg.en_font_family}'; font-size: {ov_cfg.en_font_size}pt;"
            )


class ContextViewerWindow(QWidget):
    """
    Tabbed window showing the full context sent to the translator:
    - Summary tab: the compressed story/character reference produced by the summarizer.
    - History tab: the verbatim JP→EN pairs (with scene descriptions) the LLM receives.
    """

    def __init__(self, ov_cfg: OverlayConfig, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Context Viewer")
        self.resize(520, 620)
        self._ov_cfg = ov_cfg
        self._history_entries: list[tuple[QLabel | None, QLabel, QLabel]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs)

        # --- Summary tab ---
        self._summary_edit = QPlainTextEdit()
        self._summary_edit.setReadOnly(True)
        self._summary_edit.setPlaceholderText(
            "No summary yet.\n\n"
            "Context will be summarized automatically when it grows large enough, "
            "or you can trigger it manually via right-click → Compact context."
        )
        self._tabs.addTab(self._summary_edit, "Summary")

        # --- History tab ---
        history_widget = QWidget()
        history_layout = QVBoxLayout(history_widget)
        history_layout.setContentsMargins(0, 0, 0, 0)
        self._history_scroll = QScrollArea()
        self._history_scroll.setWidgetResizable(True)
        self._history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._history_container = QWidget()
        self._history_inner = QVBoxLayout(self._history_container)
        self._history_inner.setContentsMargins(10, 8, 10, 8)
        self._history_inner.setSpacing(2)
        self._history_inner.addStretch()
        self._history_scroll.setWidget(self._history_container)
        history_layout.addWidget(self._history_scroll)
        self._tabs.addTab(history_widget, "History")

        self._apply_style()

    def _apply_style(self) -> None:
        ov = self._ov_cfg
        r, g, b, a = ov.bg_color
        er, eg, eb, ea = ov.en_color
        bg_css = f"rgba({r},{g},{b},{a})"
        en_css = f"rgba({er},{eg},{eb},{ea})"
        self._summary_edit.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background: {bg_css};"
            f"  color: {en_css};"
            f"  font-family: '{ov.en_font_family}';"
            f"  font-size: {ov.en_font_size}pt;"
            f"  border: none;"
            f"}}"
        )
        self._history_scroll.setStyleSheet(
            f"QScrollArea {{ background: {bg_css}; border: none; }}"
        )
        self._history_container.setStyleSheet(f"background: {bg_css};")
        for scene_lbl, jp_lbl, en_lbl in self._history_entries:
            self._style_entry(scene_lbl, jp_lbl, en_lbl)

    def _style_entry(
        self,
        scene_lbl: QLabel | None,
        jp_lbl: QLabel,
        en_lbl: QLabel,
    ) -> None:
        ov = self._ov_cfg
        if scene_lbl is not None:
            jr, jg, jb, ja = ov.jp_color
            er, eg, eb, ea = ov.en_color
            scene_size = max(8, int(ov.en_font_size * 0.85))
            scene_lbl.setStyleSheet(
                f"color: rgba({(jr+er)//2},{(jg+eg)//2},{(jb+eb)//2},{(ja+ea)//2});"
                f" font-style: italic;"
                f" font-family: '{ov.en_font_family}'; font-size: {scene_size}pt;"
            )
        jr, jg, jb, ja = ov.jp_color
        jp_lbl.setStyleSheet(
            f"color: rgba({jr},{jg},{jb},{ja});"
            f" font-family: '{ov.jp_font_family}'; font-size: {ov.en_font_size}pt;"
        )
        er, eg, eb, ea = ov.en_color
        en_lbl.setStyleSheet(
            f"color: rgba({er},{eg},{eb},{ea});"
            f" font-family: '{ov.en_font_family}'; font-size: {ov.en_font_size}pt;"
        )

    def set_summary(self, text: str | None) -> None:
        self._summary_edit.setPlainText(text or "")

    def set_history(self, lines: list[DialogueLine]) -> None:
        # Clear existing entries
        for scene_lbl, jp_lbl, en_lbl in self._history_entries:
            for w in (scene_lbl, jp_lbl, en_lbl):
                if w is not None:
                    w.deleteLater()
        # Remove all widgets except the trailing stretch
        while self._history_inner.count() > 1:
            item = self._history_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._history_entries.clear()

        for i, line in enumerate(lines):
            if i > 0:
                sep = QFrame(self._history_container)
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet(
                    "background-color: rgba(80,80,80,80); max-height: 1px; margin: 4px 0;"
                )
                self._history_inner.insertWidget(self._history_inner.count() - 1, sep)

            _selectable = (
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            scene_lbl: QLabel | None = None
            if line.scene_description:
                scene_lbl = QLabel(f"[Scene: {line.scene_description}]", self._history_container)
                scene_lbl.setWordWrap(True)
                scene_lbl.setTextInteractionFlags(_selectable)
                self._history_inner.insertWidget(self._history_inner.count() - 1, scene_lbl)

            jp_lbl = QLabel(line.jp, self._history_container)
            jp_lbl.setWordWrap(True)
            jp_lbl.setTextInteractionFlags(_selectable)
            en_lbl = QLabel(line.en, self._history_container)
            en_lbl.setWordWrap(True)
            en_lbl.setTextInteractionFlags(_selectable)
            self._history_inner.insertWidget(self._history_inner.count() - 1, jp_lbl)
            self._history_inner.insertWidget(self._history_inner.count() - 1, en_lbl)

            self._history_entries.append((scene_lbl, jp_lbl, en_lbl))
            self._style_entry(scene_lbl, jp_lbl, en_lbl)

    def refresh_appearance(self, ov_cfg: OverlayConfig) -> None:
        self._ov_cfg = ov_cfg
        self._apply_style()


class OverlayWindow(QWidget):
    """
    Always-on-top, semi-transparent overlay that displays Japanese text
    and its English translation.

    - Frameless, draggable, resizable via a size grip in the bottom-right corner.
    - Gear button (⚙) in the top-right corner opens the settings dialog.
    - Shows a loading indicator while translation is in progress.
    - Can be toggled visible/hidden.
    - Position, size, and colors are saved to Config.
    """

    _HANDLE_SIZE = 16  # Resize grip area in px

    def __init__(self, cfg: Config, save_fn,
                 save_context_fn, load_context_fn,
                 clear_context_fn, compact_context_fn,
                 hooker: HookerService,
                 screenshot_service: ScreenshotService,
                 get_context_fn=None) -> None:
        super().__init__()
        self._cfg = cfg
        self._save_fn = save_fn
        self._save_context_fn = save_context_fn
        self._load_context_fn = load_context_fn
        self._clear_context_fn = clear_context_fn
        self._compact_context_fn = compact_context_fn
        self._hooker = hooker
        self._screenshot_service = screenshot_service
        self._get_context_fn = get_context_fn or (lambda: (None, []))
        self._drag_pos: QPoint | None = None
        self._backlog: list[tuple[str, str]] = []
        self._backlog_win: BacklogWindow | None = None
        self._context_viewer: ContextViewerWindow | None = None
        self._retry_fn = None

        self._setup_window()
        self._setup_ui()
        self.setGeometry(
            cfg.overlay.x, cfg.overlay.y,
            cfg.overlay.width, cfg.overlay.height,
        )

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(200, 60)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # Japanese text — small, dimmed
        self._jp_label = QLabel("")
        r, g, b, a = self._cfg.overlay.jp_color
        self._jp_label.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{self._cfg.overlay.jp_font_family}'; "
            f"font-size: {self._cfg.overlay.jp_font_size}pt;"
        )
        self._jp_label.setWordWrap(True)
        self._jp_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._jp_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._jp_label)

        # English translation — larger, bright
        self._en_label = QLabel("Waiting for text...")
        r, g, b, a = self._cfg.overlay.en_color
        self._en_label.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{self._cfg.overlay.en_font_family}'; "
            f"font-size: {self._cfg.overlay.en_font_size}pt;"
        )
        self._en_label.setWordWrap(True)
        self._en_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._en_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._en_label)

        layout.addStretch()

        # Resize grip (bottom-right corner, inside layout)
        grip = _ResizeGrip(self)
        grip.setFixedSize(self._HANDLE_SIZE, self._HANDLE_SIZE)
        layout.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        # Gear button — absolutely positioned top-right, NOT in the layout
        self._gear_btn = QPushButton("⚙", self)
        self._gear_btn.setFixedSize(22, 22)
        self._gear_btn.setStyleSheet(
            "QPushButton {"
            "  color: rgba(200,200,200,160);"
            "  background: transparent;"
            "  border: none;"
            "  font-size: 14px;"
            "}"
            "QPushButton:hover { color: rgba(255,255,255,230); }"
        )
        self._gear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._gear_btn.clicked.connect(self._open_settings)

        self._exit_btn = QPushButton("✕", self)
        self._exit_btn.setFixedSize(22, 22)
        self._exit_btn.setStyleSheet(
            "QPushButton { color: rgba(200,200,200,160); background: transparent;"
            "  border: none; font-size: 13px; }"
            "QPushButton:hover { color: rgba(255,100,100,230); }"
        )
        self._exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._exit_btn.clicked.connect(QApplication.instance().quit)

        self._backlog_btn = QPushButton("≡", self)
        self._backlog_btn.setFixedSize(22, 22)
        self._backlog_btn.setStyleSheet(
            "QPushButton { color: rgba(200,200,200,160); background: transparent;"
            "  border: none; font-size: 15px; }"
            "QPushButton:hover { color: rgba(255,255,255,230); }"
        )
        self._backlog_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._backlog_btn.clicked.connect(self._toggle_backlog)

        self._context_btn = QPushButton("Σ", self)
        self._context_btn.setFixedSize(22, 22)
        self._context_btn.setStyleSheet(
            "QPushButton { color: rgba(200,200,200,160); background: transparent;"
            "  border: none; font-size: 14px; }"
            "QPushButton:hover { color: rgba(255,255,255,230); }"
        )
        self._context_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._context_btn.clicked.connect(self._toggle_context_viewer)

        self._retry_btn = QPushButton("\u21bb", self)
        self._retry_btn.setFixedSize(22, 22)
        self._retry_btn.setStyleSheet(
            "QPushButton { color: rgba(220,100,60,220); background: transparent;"
            "  border: none; font-size: 15px; }"
            "QPushButton:hover { color: rgba(255,140,80,255); }"
        )
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        self._retry_btn.hide()
        self._position_top_buttons()

        # Scene diff label — bottom-left, absolutely positioned, hidden until first capture
        self._scene_diff_label = QLabel("", self)
        self._scene_diff_label.setStyleSheet(
            "color: rgba(150,150,150,130); font-size: 9px; background: transparent;"
        )
        self._scene_diff_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._scene_diff_label.hide()

        # Summarization indicator — bottom-left, next to scene diff, hidden until triggered
        self._summ_label = QLabel("∑", self)
        self._summ_label.setStyleSheet(
            "color: rgba(220,190,80,230); font-size: 9px; background: transparent;"
        )
        self._summ_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._summ_label.hide()
        self._summ_timer = QTimer(self)
        self._summ_timer.setSingleShot(True)
        self._summ_timer.timeout.connect(self._summ_label.hide)

        # Inline context popup (right-click menu, Wayland-compatible)
        self._ctx_popup = _ContextPopup(self)
        self._ctx_popup.add_action("Save context\u2026",    self._save_context)
        self._ctx_popup.add_action("Load context\u2026",    self._load_context)
        self._ctx_popup.add_action("Clear context",         self._on_clear_context)
        self._ctx_popup.add_action("Compact context",       self._compact_context_fn)
        self._ctx_popup.add_action("Attach to process\u2026", self._attach_process)
        self._streams_btn = self._ctx_popup.add_action(
            "Select text streams\u2026", self._pick_streams
        )
        self._ctx_popup.add_action("Attach screenshot window\u2026", self._attach_screenshot)

    def _position_top_buttons(self) -> None:
        exit_x = self.width() - self._exit_btn.width() - 4
        self._exit_btn.move(exit_x, 4)
        gear_x = exit_x - self._gear_btn.width() - 4
        self._gear_btn.move(gear_x, 4)
        backlog_x = gear_x - self._backlog_btn.width() - 4
        self._backlog_btn.move(backlog_x, 4)
        context_x = backlog_x - self._context_btn.width() - 4
        self._context_btn.move(context_x, 4)
        self._retry_btn.move(context_x - self._retry_btn.width() - 4, 4)

    def _position_scene_diff_label(self) -> None:
        self._scene_diff_label.adjustSize()
        self._scene_diff_label.move(6, self.height() - self._scene_diff_label.height() - 4)

    def _position_summ_label(self) -> None:
        self._summ_label.adjustSize()
        if self._scene_diff_label.isVisible():
            x = 6 + self._scene_diff_label.width() + 6
        else:
            x = 6
        self._summ_label.move(x, self.height() - self._summ_label.height() - 4)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_text(self, jp: str, en: str, replace_last: bool = False) -> None:
        """Display a new translated line."""
        self._jp_label.setText(jp)
        self._en_label.setText(en)
        r, g, b, a = self._cfg.overlay.en_color
        self._en_label.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{self._cfg.overlay.en_font_family}'; "
            f"font-size: {self._cfg.overlay.en_font_size}pt;"
        )
        if replace_last and self._backlog:
            self._backlog[-1] = (jp, en)
            if self._backlog_win is not None:
                self._backlog_win.update_last_entry(en)
        else:
            self._backlog.append((jp, en))
            if self._backlog_win is not None:
                self._backlog_win.append_entry(jp, en)

    def show_loading(self, message: str = "Translating...") -> None:
        """Show a status indicator while an async LLM call is in flight."""
        self._retry_btn.setEnabled(False)
        self._en_label.setText(message)
        self._en_label.setStyleSheet(
            f"color: rgba(180, 180, 180, 180); "
            f"font-family: '{self._cfg.overlay.en_font_family}'; "
            f"font-size: {self._cfg.overlay.en_font_size}pt;"
        )

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def show_compact_indicator(self) -> None:
        """Flash the summarization indicator for 3 seconds (clears any error state)."""
        self._summ_label.setStyleSheet(
            "color: rgba(220,190,80,230); font-size: 9px; background: transparent;"
        )
        self._position_summ_label()
        self._summ_label.show()
        self._summ_timer.start(3000)

    def show_compact_error(self) -> None:
        """Show ∑ persistently in red to signal summarization failure."""
        self._summ_timer.stop()
        self._summ_label.setStyleSheet(
            "color: rgba(220,80,80,230); font-size: 9px; background: transparent;"
        )
        self._position_summ_label()
        self._summ_label.show()

    def show_error_text(self, jp: str, message: str) -> None:
        """Display an error message without adding it to the backlog."""
        self._jp_label.setText(jp)
        self._en_label.setText(message)
        r, g, b, a = self._cfg.overlay.en_color
        self._en_label.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{self._cfg.overlay.en_font_family}'; "
            f"font-size: {self._cfg.overlay.en_font_size}pt;"
        )

    def show_retry_button(self, retry_fn, is_error: bool = False) -> None:
        """Show the ↻ retry button; retry_fn is a zero-arg callable."""
        self._retry_fn = retry_fn
        if is_error:
            self._retry_btn.setStyleSheet(
                "QPushButton { color: rgba(220,100,60,220); background: transparent;"
                "  border: none; font-size: 15px; }"
                "QPushButton:hover { color: rgba(255,140,80,255); }"
            )
        else:
            self._retry_btn.setStyleSheet(
                "QPushButton { color: rgba(200,200,200,160); background: transparent;"
                "  border: none; font-size: 15px; }"
                "QPushButton:hover { color: rgba(255,255,255,230); }"
            )
        self._retry_btn.setEnabled(True)
        self._position_top_buttons()
        self._retry_btn.show()

    def hide_retry_button(self) -> None:
        """Hide the ↻ retry button and clear any pending retry."""
        self._retry_btn.hide()
        self._retry_fn = None

    def set_scene_describe_error(self) -> None:
        """Turn the Δ label red to signal a describe_scene failure."""
        self._scene_diff_label.setStyleSheet(
            "color: rgba(220,80,80,200); font-size: 9px; background: transparent;"
        )
        self._scene_diff_label.setText("Δ ●")
        self._position_scene_diff_label()
        self._scene_diff_label.show()

    def _on_retry_clicked(self) -> None:
        if self._retry_fn is not None:
            self._retry_fn()

    def set_screenshot_enabled(self, enabled: bool) -> None:
        """Hide the scene diff label when screenshot context is turned off."""
        if not enabled:
            self._scene_diff_label.hide()

    def set_scene_diff(self, pct: float, triggered: bool) -> None:
        """Update the bottom-left scene diff indicator."""
        if triggered:
            self._scene_diff_label.setStyleSheet(
                "color: rgba(160,220,160,200); font-size: 9px; background: transparent;"
            )
            self._scene_diff_label.setText(f"Δ {pct:.1f}% ●")
        else:
            self._scene_diff_label.setStyleSheet(
                "color: rgba(150,150,150,130); font-size: 9px; background: transparent;"
            )
            self._scene_diff_label.setText(f"Δ {pct:.1f}%")
        self._position_scene_diff_label()
        self._scene_diff_label.show()

    def save_geometry_to_config(self) -> None:
        """Update the OverlayConfig with the current window geometry."""
        geo = self.geometry()
        self._cfg.overlay.x = geo.x()
        self._cfg.overlay.y = geo.y()
        self._cfg.overlay.width = geo.width()
        self._cfg.overlay.height = geo.height()

    def apply_appearance(self, ov: OverlayConfig) -> None:
        """Apply appearance settings live. Called by SettingsDialog during preview."""
        self._cfg.overlay.bg_color     = list(ov.bg_color)
        self._cfg.overlay.jp_color     = list(ov.jp_color)
        self._cfg.overlay.en_color     = list(ov.en_color)
        self._cfg.overlay.jp_font_family = ov.jp_font_family
        self._cfg.overlay.jp_font_size   = ov.jp_font_size
        self._cfg.overlay.en_font_family = ov.en_font_family
        self._cfg.overlay.en_font_size   = ov.en_font_size
        r, g, b, a = ov.jp_color
        self._jp_label.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{ov.jp_font_family}'; "
            f"font-size: {ov.jp_font_size}pt;"
        )
        r, g, b, a = ov.en_color
        self._en_label.setStyleSheet(
            f"color: rgba({r},{g},{b},{a}); "
            f"font-family: '{ov.en_font_family}'; "
            f"font-size: {ov.en_font_size}pt;"
        )
        self.update()  # repaint background
        if self._backlog_win is not None:
            self._backlog_win.refresh_appearance(ov)
        if self._context_viewer is not None:
            self._context_viewer.refresh_appearance(ov)

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    _CONTEXT_DIR = os.path.expanduser("~/.config/vntl/contexts/")

    def _save_context(self) -> None:
        os.makedirs(self._CONTEXT_DIR, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save context", self._CONTEXT_DIR, "JSON files (*.json)"
        )
        if path:
            self._save_context_fn(path)

    def _load_context(self) -> None:
        os.makedirs(self._CONTEXT_DIR, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load context", self._CONTEXT_DIR, "JSON files (*.json)"
        )
        if path:
            self._load_context_fn(path)
            summary, history = self._get_context_fn()
            self.update_context(summary, history)

    def _on_clear_context(self) -> None:
        self._clear_context_fn()
        self.update_context(None, [])

    def _attach_process(self) -> None:
        dlg = ProcessPickerDialog(
            self._hooker.attach,
            self,
            fail_msg=(
                "Could not inject the hook DLL into PID {pid}.\n\n"
                "Make sure VNTL is running as administrator and that the DLLs\n"
                "in the hook/ directory are present (run `make -C hook` to build)."
            ),
        )
        dlg.exec()

    def _attach_screenshot(self) -> None:
        def do_attach(pid: int) -> bool:
            self._screenshot_service.set_pid(pid)
            return True
        dlg = ProcessPickerDialog(do_attach, self, title="Attach screenshot window")
        dlg.exec()

    def _pick_streams(self) -> None:
        dlg = StreamPickerDialog(self._hooker, self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Backlog
    # ------------------------------------------------------------------

    def _toggle_backlog(self) -> None:
        if self._backlog_win is None:
            self._backlog_win = BacklogWindow(self._cfg.overlay)
            for jp, en in self._backlog:
                self._backlog_win.append_entry(jp, en)
        if self._backlog_win.isVisible():
            self._backlog_win.hide()
        else:
            self._backlog_win.show()
            self._backlog_win.raise_()

    # ------------------------------------------------------------------
    # Context viewer (Σ)
    # ------------------------------------------------------------------

    def _toggle_context_viewer(self) -> None:
        if self._context_viewer is None:
            self._context_viewer = ContextViewerWindow(self._cfg.overlay)
            summary, history = self._get_context_fn()
            self._context_viewer.set_summary(summary)
            self._context_viewer.set_history(history)
        if self._context_viewer.isVisible():
            self._context_viewer.hide()
        else:
            self._context_viewer.show()
            self._context_viewer.raise_()

    def update_context(self, summary: str | None, history: list) -> None:
        if self._context_viewer is not None:
            self._context_viewer.set_summary(summary)
            self._context_viewer.set_history(history)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        from settings import SettingsDialog  # lazy import avoids circular
        SettingsDialog(self._cfg, self, self._save_fn, self._screenshot_service, self._hooker).exec()

    # ------------------------------------------------------------------
    # Painting — dark rounded background
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, g, b, a = self._cfg.overlay.bg_color
        painter.setBrush(QColor(r, g, b, a))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 8, 8)
        painter.end()

    # ------------------------------------------------------------------
    # Dragging
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._ctx_popup.popup_at(event.pos())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle and handle.startSystemMove():
                self._drag_pos = None
                return
            # X11 / XWayland fallback
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        # Only reached on X11 fallback (Wayland move is handled by the compositor)
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.save_geometry_to_config()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.save_geometry_to_config()
        self._position_top_buttons()
        self._position_scene_diff_label()
        self._position_summ_label()


# ---------------------------------------------------------------------------
# Process picker dialog
# ---------------------------------------------------------------------------

class ProcessPickerDialog(QDialog):
    """
    Generic process-picker dialog.  Calls attach_fn(pid) when the user
    confirms; shows an error message (with {pid} substituted) if it returns False.
    """

    def __init__(
        self,
        attach_fn,
        parent: QWidget | None = None,
        title: str = "Attach to process",
        fail_msg: str = "Could not attach to PID {pid}.",
    ) -> None:
        super().__init__(parent)
        self._attach_fn = attach_fn
        self._fail_msg = fail_msg
        self.setWindowTitle(title)
        self.setMinimumSize(380, 420)
        self._build_ui()
        self._populate()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 10, 10, 10)

        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("Filter by name…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget(self)
        self._list.setAlternatingRowColors(True)
        self._list.itemDoubleClicked.connect(self._do_attach)
        layout.addWidget(self._list)

        btns = QDialogButtonBox(self)
        self._attach_btn = btns.addButton("Attach", QDialogButtonBox.ButtonRole.AcceptRole)
        refresh_btn      = btns.addButton("Refresh", QDialogButtonBox.ButtonRole.ResetRole)
        cancel_btn       = btns.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._attach_btn.clicked.connect(self._do_attach)
        refresh_btn.clicked.connect(self._populate)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(btns)

    def _populate(self) -> None:
        self._all_items: list[tuple[int, str]] = sorted(
            list_processes(), key=lambda x: x[1].lower()
        )
        self._apply_filter(self._filter.text())

    def _apply_filter(self, text: str) -> None:
        self._list.clear()
        needle = text.strip().lower()
        for pid, name in self._all_items:
            if needle and needle not in name.lower():
                continue
            item = QListWidgetItem(f"{name}  [{pid}]")
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self._list.addItem(item)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _do_attach(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        pid: int = item.data(Qt.ItemDataRole.UserRole)
        if self._attach_fn(pid):
            self.accept()
        else:
            QMessageBox.warning(self, "Attach failed", self._fail_msg.format(pid=pid))


# ---------------------------------------------------------------------------
# Stream picker dialog
# ---------------------------------------------------------------------------

class StreamPickerDialog(QDialog):
    """
    Dialog for selecting which text streams to combine for translation.

    Each stream corresponds to a unique call-site (return address) in the
    game that calls a GDI text function.  The user can:
      - Check/uncheck streams to include in the combined output.
      - Drag rows to reorder them (order determines combination order).
      - Choose a separator inserted between combined stream texts.

    Unchecking all streams and clicking OK reverts to passthrough mode
    (all captured text flows through individually, legacy behaviour).
    """

    _SEPARATOR_OPTIONS: list[tuple[str, str]] = [
        ("Newline", "\n"),
        ("Space",   " "),
        ("None",    ""),
    ]

    def __init__(self, hooker: HookerService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hooker = hooker
        self.setWindowTitle("Select text streams")
        self.setMinimumSize(520, 400)
        self._build_ui()
        self._refresh()
        # Auto-refresh every 2 s while the dialog is open
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        hint = QLabel(
            "Check streams to include. Drag rows to set combination order.",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(hint)

        self._status_label = QLabel(self)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("font-size: 11px; padding: 2px 0;")
        layout.addWidget(self._status_label)

        self._list = QListWidget(self)
        self._list.setAlternatingRowColors(True)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self._list)

        # Separator row
        sep_row = QHBoxLayout()
        sep_row.setSpacing(12)
        sep_row.addWidget(QLabel("Separator:", self))
        self._sep_group = QButtonGroup(self)
        current_sep = self._hooker._separator
        for i, (label, value) in enumerate(self._SEPARATOR_OPTIONS):
            rb = QRadioButton(label, self)
            if value == current_sep:
                rb.setChecked(True)
            self._sep_group.addButton(rb, i)
            sep_row.addWidget(rb)
        sep_row.addStretch()
        layout.addLayout(sep_row)

        # Buttons
        btns = QDialogButtonBox(self)
        select_all_btn = btns.addButton("Select All", QDialogButtonBox.ButtonRole.ResetRole)
        refresh_btn    = btns.addButton("Refresh",    QDialogButtonBox.ButtonRole.ResetRole)
        ok_btn         = btns.addButton(QDialogButtonBox.StandardButton.Ok)
        cancel_btn     = btns.addButton(QDialogButtonBox.StandardButton.Cancel)
        select_all_btn.clicked.connect(self._select_all)
        refresh_btn.clicked.connect(self._refresh)
        ok_btn.clicked.connect(self._apply)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(btns)

    # ------------------------------------------------------------------
    # Refresh list from live stream data
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Rebuild the list from the hooker's current streams."""
        if self._hooker.is_attached:
            streams = self._hooker.get_streams()
            if streams:
                self._status_label.setText(
                    f"\u2714 Connected \u2014 {len(streams)} stream(s) detected."
                )
            else:
                self._status_label.setText(
                    "\u2714 Connected \u2014 no text captured yet. "
                    "Advance dialogue in the game to see streams."
                )
            self._status_label.setStyleSheet("color: #6c6; font-size: 11px; padding: 2px 0;")
        else:
            self._status_label.setText(
                "\u26a0 Not connected \u2014 use \u2018Attach to process\u2026\u2019 first."
            )
            self._status_label.setStyleSheet("color: #c96; font-size: 11px; padding: 2px 0;")

        if not self._hooker.is_attached:
            streams = []
        enabled = self._hooker._enabled_streams or []

        # Remember which hook_ids are currently checked in the widget
        # (user may have edited checkboxes before auto-refresh fires)
        widget_checked: set[int] = set()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                widget_checked.add(item.data(Qt.ItemDataRole.UserRole))

        # Collect hook_ids currently in widget (for detecting new arrivals)
        widget_ids: set[int] = {
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
        }

        # Add newly-seen streams at the bottom; update sample text for existing ones
        new_stream_ids = {s.hook_id for s in streams} - widget_ids
        for stream in streams:
            if stream.hook_id in new_stream_ids:
                item = self._make_item(stream, enabled)
                self._list.addItem(item)
            else:
                # Update sample text for existing rows
                for i in range(self._list.count()):
                    existing = self._list.item(i)
                    if existing.data(Qt.ItemDataRole.UserRole) == stream.hook_id:
                        existing.setText(self._item_label(stream))
                        # Preserve user's checkbox state from widget
                        if stream.hook_id in widget_checked:
                            existing.setCheckState(Qt.CheckState.Checked)
                        break

        if not streams:
            if self._list.count() == 0:
                msg = (
                    "Waiting for text…"
                    if self._hooker.is_attached
                    else "Not attached to any process."
                )
                placeholder = QListWidgetItem(msg)
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                self._list.addItem(placeholder)

    def _item_label(self, stream: StreamState) -> str:
        sample = "  /  ".join(stream.samples[-3:]) if stream.samples else "(no samples yet)"
        return f"0x{stream.hook_id:016X}   {sample}"

    def _make_item(self, stream: StreamState, enabled: list[int]) -> QListWidgetItem:
        item = QListWidgetItem(self._item_label(stream))
        item.setData(Qt.ItemDataRole.UserRole, stream.hook_id)
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        checked = stream.hook_id in enabled
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        return item

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _select_all(self) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is not None:
                item.setCheckState(Qt.CheckState.Checked)

    def _apply(self) -> None:
        """Collect checked items in list order and apply to hooker."""
        checked_ids: list[int] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            hid = item.data(Qt.ItemDataRole.UserRole)
            if hid is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                checked_ids.append(hid)

        self._hooker.set_enabled_streams(checked_ids)

        # Apply separator selection
        idx = self._sep_group.checkedId()
        if 0 <= idx < len(self._SEPARATOR_OPTIONS):
            _, sep_value = self._SEPARATOR_OPTIONS[idx]
            self._hooker.set_separator(sep_value)

        self.accept()

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)
