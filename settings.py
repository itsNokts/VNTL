from __future__ import annotations

import copy
import math
import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFontComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config import Config, OverlayConfig, ROLE_PROVIDER_DEFAULTS, ROLE_MODEL_OPTIONS
from translator import _SYSTEM_PROMPT, _SUMMARIZE_SYSTEM, _DESCRIBE_SYSTEM


def _slider_to_threshold(pos: int) -> float:
    """Map slider position 0–100 to a threshold % on a log scale.
    pos=0  → 0.0%   (always trigger)
    pos=33 → 1.0%   (expression-level changes)
    pos=50 → 3.2%   (moderate changes / sprite swaps)
    pos=67 → 10.0%  (scene transitions)
    pos=100 → 100%
    """
    if pos <= 0:
        return 0.0
    return 10 ** (pos * 3 / 100 - 1)


def _threshold_to_slider(pct: float) -> int:
    """Inverse of _slider_to_threshold."""
    if pct <= 0.0:
        return 0
    pos = (math.log10(pct) + 1) * 100 / 3
    return max(1, min(100, round(pos)))

def _ensure_arrow_images() -> tuple[str, str]:
    """
    Generate up/down arrow PNGs using Pillow and return (up_path, down_path).
    Paths use forward slashes for Qt CSS compatibility on Windows.
    Returns ('', '') if Pillow is not installed.
    """
    config_dir = os.path.expanduser("~/.config/vntl")
    os.makedirs(config_dir, exist_ok=True)
    up_path   = os.path.join(config_dir, "arrow_up.png")
    down_path = os.path.join(config_dir, "arrow_down.png")
    try:
        from PIL import Image
        color = (170, 170, 204, 255)  # #aaaacc opaque
        w, h, cx = 9, 5, 4

        def _make(pointing_up: bool, path: str) -> None:
            if os.path.exists(path):
                return
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            px  = img.load()
            for row in range(h):
                half = row if pointing_up else (h - 1 - row)
                for x in range(cx - half, cx + half + 1):
                    if 0 <= x < w:
                        px[x, row] = color
            img.save(path)

        _make(True,  up_path)
        _make(False, down_path)
        return (
            up_path.replace("\\", "/"),
            down_path.replace("\\", "/"),
        )
    except ImportError:
        return ("", "")


_UP_ARROW_PATH, _DOWN_ARROW_PATH = _ensure_arrow_images()

_DARK_BG = "#141419"

_STYLE = f"""
    QDialog {{
        background-color: {_DARK_BG};
    }}
    QWidget {{
        background-color: transparent;
        color: #ffffff;
    }}
    QLabel {{
        color: #ffffff;
    }}
    QLineEdit {{
        background-color: #1e1e26;
        color: #ffffff;
        border: 1px solid #3a3a4a;
        border-radius: 4px;
        padding: 4px 6px;
    }}
    QComboBox {{
        background-color: #1e1e26;
        color: #ffffff;
        border: 1px solid #3a3a4a;
        border-radius: 4px;
        padding: 4px 6px;
        min-width: 120px;
    }}
    QComboBox::drop-down {{
        border: none;
        width: 20px;
    }}
    QComboBox::down-arrow {{
        image: url("{_DOWN_ARROW_PATH}");
        width: 9px;
        height: 5px;
    }}
    QComboBox QAbstractItemView {{
        background-color: #1e1e26;
        color: #ffffff;
        selection-background-color: #2e2e42;
        border: 1px solid #3a3a4a;
    }}
    QAbstractItemView {{
        background-color: #1e1e26;
        color: #ffffff;
        border: 1px solid #3a3a4a;
        outline: 0px;
    }}
    QAbstractScrollArea > QWidget {{
        background-color: #1e1e26;
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: #3a3a4a;
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: #7070c0;
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }}
    QSlider::sub-page:horizontal {{
        background: #5050a0;
        border-radius: 2px;
    }}
    QCheckBox {{
        color: #ffffff;
        spacing: 6px;
    }}
    QCheckBox::indicator {{
        background-color: #1e1e26;
        border: 1px solid #3a3a4a;
        border-radius: 3px;
        width: 14px;
        height: 14px;
    }}
    QCheckBox::indicator:checked {{
        background-color: #5050a0;
        border-color: #7070c0;
    }}
    QSpinBox {{
        background-color: #1e1e26;
        color: #ffffff;
        border: 1px solid #3a3a4a;
        border-radius: 4px;
        padding: 4px 24px 4px 6px;
        min-width: 60px;
        max-width: 72px;
    }}
    QSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 18px;
        border-left: 1px solid #3a3a4a;
        border-top-right-radius: 4px;
        background-color: #2a2a36;
    }}
    QSpinBox::up-button:hover {{
        background-color: #3a3a52;
    }}
    QSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 18px;
        border-left: 1px solid #3a3a4a;
        border-bottom-right-radius: 4px;
        background-color: #2a2a36;
    }}
    QSpinBox::down-button:hover {{
        background-color: #3a3a52;
    }}
    QSpinBox::up-arrow {{
        image: url("{_UP_ARROW_PATH}");
        width: 9px;
        height: 5px;
    }}
    QSpinBox::down-arrow {{
        image: url("{_DOWN_ARROW_PATH}");
        width: 9px;
        height: 5px;
    }}
    QTabWidget::pane {{
        border: 1px solid #3a3a4a;
        border-radius: 4px;
        background-color: transparent;
    }}
    QTabBar::tab {{
        background-color: #1e1e26;
        color: #aaaacc;
        padding: 6px 18px;
        border: 1px solid #3a3a4a;
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background-color: #2a2a3a;
        color: #ffffff;
    }}
    QTabBar::tab:hover:!selected {{
        background-color: #242432;
        color: #ccccee;
    }}
    QTextEdit {{
        background-color: #1e1e26;
        color: #ffffff;
        border: 1px solid #3a3a4a;
        border-radius: 4px;
        padding: 4px 6px;
    }}
"""

_BTN_SWATCH = (
    "QPushButton {{"
    "  background-color: rgba({r},{g},{b},{a});"
    "  border: 1px solid #3a3a4a;"
    "  border-radius: 4px;"
    "  min-width: 48px; max-width: 48px;"
    "  min-height: 22px; max-height: 22px;"
    "}}"
    "QPushButton:hover {{ border-color: #7070c0; }}"
)

_BTN_ACTION = (
    "QPushButton {"
    "  background-color: #2e2e3e;"
    "  color: #ffffff;"
    "  border: 1px solid #3a3a4a;"
    "  border-radius: 4px;"
    "  padding: 6px 20px;"
    "}"
    "QPushButton:hover { background-color: #3e3e52; }"
)

_BTN_SAVE = (
    "QPushButton {"
    "  background-color: #3a3a80;"
    "  color: #ffffff;"
    "  border: 1px solid #5050a0;"
    "  border-radius: 4px;"
    "  padding: 6px 20px;"
    "}"
    "QPushButton:hover { background-color: #5050a0; }"
)

# Display name → internal provider key
_PROVIDER_KEY_MAP: dict[str, str] = {
    "Anthropic": "anthropic",
    "OpenAI":    "openai",
    "Google":    "google",
    "Ollama":    "ollama",
}
_PROVIDERS = list(_PROVIDER_KEY_MAP.keys())


def _section_header(text: str) -> QLabel:
    lbl = QLabel(text)
    font = QFont()
    font.setBold(True)
    font.setPointSize(10)
    lbl.setFont(font)
    lbl.setStyleSheet("color: rgba(160,160,220,255); margin-top: 6px;")
    return lbl


def _subsection_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color: rgba(140,140,200,200); font-size: 8pt; margin-top: 4px;")
    return lbl


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("background-color: #2a2a3a; max-height: 1px;")
    return line


class SettingsDialog(QDialog):
    """
    Dark-themed settings dialog opened from the overlay's gear button.

    Appearance changes (colors, opacity) are applied live to the overlay.
    Backend changes are written to config on Save and take effect on next launch.
    Cancel reverts all live appearance changes.
    """

    def __init__(self, cfg: Config, overlay, save_fn, screenshot_service=None) -> None:
        super().__init__(overlay)

        self._cfg = cfg
        self._overlay = overlay
        self._save_fn = save_fn
        self._screenshot_service = screenshot_service

        # Snapshot current appearance so Cancel can revert exactly
        self._orig_overlay: OverlayConfig = copy.deepcopy(cfg.overlay)

        self.setWindowTitle("VNTL Settings")
        self.setMinimumWidth(520)
        self.resize(540, 650)
        self.setStyleSheet(_STYLE)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint
        )

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(18, 16, 18, 16)

        tabs = QTabWidget()

        for title, builder in [
            ("Appearance",  self._build_appearance),
            ("Translation", self._build_models_and_apis),
            ("Context",     self._build_context),
            ("Prompts",     self._build_prompts),
        ]:
            page = QWidget()
            page.setStyleSheet("background-color: transparent;")
            lay = QVBoxLayout(page)
            lay.setSpacing(8)
            lay.setContentsMargins(12, 12, 12, 12)
            builder(lay)
            lay.addStretch()
            tabs.addTab(page, title)

        root.addWidget(tabs)
        root.addWidget(_separator())
        self._build_buttons(root)
        self._fix_combo_popups()

    def _fix_combo_popups(self) -> None:
        """Force every QComboBox popup in the dialog to be opaque dark."""
        bg  = QColor("#1e1e26")
        fg  = QColor("#ffffff")
        sel = QColor("#2e2e42")
        for combo in self.findChildren(QComboBox):
            view = combo.view()
            view.setAutoFillBackground(True)
            p = QPalette(view.palette())
            p.setColor(QPalette.ColorRole.Base,            bg)
            p.setColor(QPalette.ColorRole.AlternateBase,   bg)
            p.setColor(QPalette.ColorRole.Window,          bg)
            p.setColor(QPalette.ColorRole.Text,            fg)
            p.setColor(QPalette.ColorRole.Highlight,       sel)
            p.setColor(QPalette.ColorRole.HighlightedText, fg)
            view.setPalette(p)

    def _build_appearance(self, parent: QVBoxLayout) -> None:
        # Opacity slider
        row = QHBoxLayout()
        lbl = QLabel("Background opacity:")
        lbl.setFixedWidth(165)
        row.addWidget(lbl)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(int(self._cfg.overlay.bg_color[3] / 255 * 100))
        row.addWidget(self._opacity_slider)

        self._opacity_lbl = QLabel(f"{int(self._cfg.overlay.bg_color[3] / 255 * 100)}%")
        self._opacity_lbl.setFixedWidth(36)
        self._opacity_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._opacity_lbl)

        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        parent.addLayout(row)

        # Color rows
        self._bg_btn = self._add_color_row(
            parent, "Background color:", self._cfg.overlay.bg_color,
            self._set_bg_color,
        )
        self._jp_btn = self._add_color_row(
            parent, "Japanese text color:", self._cfg.overlay.jp_color,
            self._set_jp_color,
        )
        self._en_btn = self._add_color_row(
            parent, "English text color:", self._cfg.overlay.en_color,
            self._set_en_color,
        )

        # Font rows
        for attr_family, attr_size, row_label in [
            ("jp_font_family", "jp_font_size", "Japanese font:"),
            ("en_font_family", "en_font_size", "English font:"),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(row_label)
            lbl.setFixedWidth(165)
            row.addWidget(lbl)

            size_spin = QSpinBox()
            size_spin.setRange(6, 72)
            size_spin.setValue(getattr(self._cfg.overlay, attr_size))
            row.addWidget(size_spin)

            font_combo = QFontComboBox()
            font_combo.setEditable(False)
            font_combo.setCurrentFont(QFont(getattr(self._cfg.overlay, attr_family)))
            row.addWidget(font_combo)

            parent.addLayout(row)

            font_combo.currentFontChanged.connect(
                lambda f, a=attr_family: self._on_font_changed(a, f.family()))
            size_spin.valueChanged.connect(
                lambda v, a=attr_size: self._on_font_changed(a, v))

    def _add_color_row(
        self,
        parent: QVBoxLayout,
        label: str,
        color: list[int],
        setter,
    ) -> QPushButton:
        """Add a label + color-swatch button row. Returns the swatch button."""
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(165)
        row.addWidget(lbl)

        btn = QPushButton()
        self._refresh_swatch(btn, color)
        btn.clicked.connect(lambda: self._pick_color(btn, setter))
        row.addWidget(btn)
        row.addStretch()

        parent.addLayout(row)
        return btn

    def _build_context(self, parent: QVBoxLayout) -> None:
        # Clipboard toggle
        self._clipboard_check = QCheckBox("Translate from clipboard")
        self._clipboard_check.setChecked(self._cfg.clipboard_enabled)
        parent.addWidget(self._clipboard_check)

        cb_note = QLabel(
            "Disable if you copy text while reading and don't want it auto-translated."
        )
        cb_note.setStyleSheet(
            "color: rgba(160,160,160,180); font-size: 8pt; font-style: italic;"
        )
        parent.addWidget(cb_note)

        # Screenshot toggle
        self._screenshot_check = QCheckBox(
            "Screenshot context  (descriptor model must support vision)"
        )
        self._screenshot_check.setChecked(self._cfg.screenshot_enabled)
        parent.addWidget(self._screenshot_check)

        sc_note = QLabel(
            "~500\u2013800 extra tokens per new scene. "
            "Use \u2018Attach screenshot window\u2026\u2019 in the right-click menu to select the game window."
        )
        sc_note.setStyleSheet(
            "color: rgba(160,160,160,180); font-size: 8pt; font-style: italic;"
        )
        parent.addWidget(sc_note)

        # Scene change threshold slider
        parent.addSpacing(4)
        thresh_row = QHBoxLayout()
        lbl_thresh = QLabel("Scene change threshold:")
        lbl_thresh.setFixedWidth(165)
        thresh_row.addWidget(lbl_thresh)

        self._scene_thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._scene_thresh_slider.setRange(0, 100)
        self._scene_thresh_slider.setValue(_threshold_to_slider(self._cfg.scene_change_threshold))
        thresh_row.addWidget(self._scene_thresh_slider)

        _init_pct = _slider_to_threshold(self._scene_thresh_slider.value())
        self._scene_thresh_lbl = QLabel(f"{_init_pct:.1f}%")
        self._scene_thresh_lbl.setFixedWidth(48)
        self._scene_thresh_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        thresh_row.addWidget(self._scene_thresh_lbl)

        self._scene_thresh_slider.valueChanged.connect(
            lambda v: self._scene_thresh_lbl.setText(f"{_slider_to_threshold(v):.1f}%")
        )
        parent.addLayout(thresh_row)

        thresh_note = QLabel("Log scale: 0 = always, pos~33 = 1% (expressions), pos~50 = 3% (sprites), pos~67 = 10% (transitions).")
        thresh_note.setStyleSheet(
            "color: rgba(160,160,160,180); font-size: 8pt; font-style: italic;"
        )
        parent.addWidget(thresh_note)

        parent.addSpacing(4)

        # Summarize-at-tokens
        tok_row = QHBoxLayout()
        lbl = QLabel("Summarize context at:")
        lbl.setFixedWidth(165)
        tok_row.addWidget(lbl)
        self._summ_spin = QSpinBox()
        self._summ_spin.setRange(1_000, 500_000)
        self._summ_spin.setSingleStep(1_000)
        self._summ_spin.setValue(self._cfg.context_summarize_tokens)
        self._summ_spin.setMaximumWidth(110)
        tok_row.addWidget(self._summ_spin)
        tok_row.addWidget(QLabel("tokens"))
        tok_row.addStretch()
        parent.addLayout(tok_row)

        # Min recent lines
        lines_row = QHBoxLayout()
        lbl2 = QLabel("Keep verbatim lines:")
        lbl2.setFixedWidth(165)
        lines_row.addWidget(lbl2)
        self._lines_spin = QSpinBox()
        self._lines_spin.setRange(5, 100)
        self._lines_spin.setValue(self._cfg.context_min_recent_lines)
        lines_row.addWidget(self._lines_spin)
        lines_row.addWidget(QLabel("lines"))
        lines_row.addStretch()
        parent.addLayout(lines_row)


    def _build_models_and_apis(self, parent: QVBoxLayout) -> None:
        # --- API Keys subsection ---
        parent.addWidget(_subsection_label("API Keys"))

        parent.addLayout(self._input_row(
            "Anthropic key:", self._cfg.anthropic_api_key,
            password=True, dest="_anthropic_key_edit",
        ))
        parent.addLayout(self._input_row(
            "OpenAI key:", self._cfg.openai_api_key,
            password=True, dest="_openai_key_edit",
        ))
        parent.addLayout(self._input_row(
            "Google key:", self._cfg.google_api_key,
            password=True, dest="_google_key_edit",
        ))
        parent.addLayout(self._input_row(
            "Ollama URL:", self._cfg.ollama_base_url,
            password=False, dest="_ollama_url_edit",
        ))

        parent.addSpacing(6)

        # --- Roles subsection ---
        parent.addWidget(_subsection_label("Roles  (provider + model)"))

        self._role_combos:        dict[str, QComboBox]      = {}
        self._role_model_combos:  dict[str, QComboBox]      = {}
        self._role_model_edits:   dict[str, QLineEdit]      = {}
        self._role_model_stacks:  dict[str, QStackedWidget] = {}
        self._role_think_checks:  dict[str, QCheckBox]      = {}
        self._role_max_tokens_spins: dict[str, QSpinBox]    = {}

        for role_key, label, provider_attr, model_attr, thinking_attr in [
            ("translator", "Translator:", "translator_provider", "translator_model", "translator_ollama_thinking"),
            ("descriptor", "Descriptor:", "descriptor_provider", "descriptor_model", "descriptor_ollama_thinking"),
            ("summarizer", "Summarizer:", "summarizer_provider", "summarizer_model", "summarizer_ollama_thinking"),
        ]:
            provider_val = getattr(self._cfg, provider_attr)
            model_val    = getattr(self._cfg, model_attr)
            options      = ROLE_MODEL_OPTIONS.get(role_key, {}).get(provider_val, [])

            row = QHBoxLayout()

            lbl = QLabel(label)
            lbl.setFixedWidth(90)
            row.addWidget(lbl)

            # Provider combo
            prov_combo = QComboBox()
            prov_combo.addItems(_PROVIDERS)
            prov_combo.setFixedWidth(110)
            display = next(
                (k for k, v in _PROVIDER_KEY_MAP.items() if v == provider_val),
                "Anthropic",
            )
            prov_combo.setCurrentText(display)
            row.addWidget(prov_combo)

            # Model combo (page 0 of stack)
            model_combo = QComboBox()
            model_combo.addItems(options)
            model_combo.addItem("Other\u2026")

            # Other text field (page 1 of stack)
            model_edit = QLineEdit()
            model_edit.setPlaceholderText("Enter model name\u2026")

            # Choose initial stack page
            if model_val in options:
                model_combo.setCurrentText(model_val)
                initial_idx = 0
            else:
                model_combo.setCurrentText("Other\u2026")
                model_edit.setText(model_val)
                initial_idx = 1

            stack = QStackedWidget()
            stack.addWidget(model_combo)
            stack.addWidget(model_edit)
            stack.setCurrentIndex(initial_idx)
            stack.setFixedHeight(prov_combo.sizeHint().height())
            row.addWidget(stack)

            think_check = QCheckBox("Think")
            think_check.setChecked(getattr(self._cfg, thinking_attr))
            think_check.setEnabled(provider_val == "ollama")
            row.addWidget(think_check)

            parent.addLayout(row)

            # Max output tokens sub-row (indented under the provider/model row)
            tok_row = QHBoxLayout()
            spacer = QLabel()
            spacer.setFixedWidth(90)
            tok_row.addWidget(spacer)
            lbl_tok = QLabel("Max output tokens:")
            lbl_tok.setFixedWidth(130)
            tok_row.addWidget(lbl_tok)
            tok_spin = QSpinBox()
            tok_spin.setRange(128, 16384)
            tok_spin.setSingleStep(128)
            tok_spin.setValue(getattr(self._cfg, f"{role_key}_max_tokens"))
            tok_spin.setMaximumWidth(80)
            tok_row.addWidget(tok_spin)
            tok_row.addStretch()
            parent.addLayout(tok_row)

            self._role_combos[role_key]          = prov_combo
            self._role_model_combos[role_key]    = model_combo
            self._role_model_edits[role_key]     = model_edit
            self._role_model_stacks[role_key]    = stack
            self._role_think_checks[role_key]    = think_check
            self._role_max_tokens_spins[role_key] = tok_spin

            # Switch to text field when "Other…" is selected
            def _on_model_combo_changed(
                text: str, stk: QStackedWidget = stack, edit: QLineEdit = model_edit,
            ) -> None:
                if text == "Other\u2026":
                    stk.setCurrentIndex(1)
                    edit.clear()
                    edit.setFocus()

            model_combo.currentTextChanged.connect(_on_model_combo_changed)

            # Rebuild model options and auto-fill when provider changes
            def _on_provider_changed(
                text: str,
                stk:  QStackedWidget = stack,
                mc:   QComboBox      = model_combo,
                edit: QLineEdit      = model_edit,
                rk:   str            = role_key,
                chk:  QCheckBox      = think_check,
            ) -> None:
                new_internal = _PROVIDER_KEY_MAP.get(text, "anthropic")
                chk.setEnabled(new_internal == "ollama")
                new_options  = ROLE_MODEL_OPTIONS.get(rk, {}).get(new_internal, [])
                new_default  = ROLE_PROVIDER_DEFAULTS.get(rk, {}).get(new_internal, "")
                all_defaults = {
                    v for rd in ROLE_PROVIDER_DEFAULTS.values() for v in rd.values()
                }

                mc.blockSignals(True)
                mc.clear()
                mc.addItems(new_options)
                mc.addItem("Other\u2026")
                mc.blockSignals(False)

                if stk.currentIndex() == 1:
                    # Other mode: switch back to combo if typed value is now an option
                    current = edit.text().strip()
                    if current in new_options:
                        mc.setCurrentText(current)
                        stk.setCurrentIndex(0)
                else:
                    current = mc.currentText()
                    if (current == "" or current == "Other\u2026"
                            or current in all_defaults
                            or current not in new_options):
                        if new_default in new_options:
                            mc.setCurrentText(new_default)
                        stk.setCurrentIndex(0)

            prov_combo.currentTextChanged.connect(_on_provider_changed)


    def _input_row(
        self, label: str, value: str, *, password: bool = False, dest: str
    ) -> QHBoxLayout:
        """Build a label + QLineEdit row and store the edit as self.<dest>."""
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(165)
        row.addWidget(lbl)

        edit = QLineEdit(value)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(edit)

        setattr(self, dest, edit)
        return row

    def _build_prompts(self, parent: QVBoxLayout) -> None:
        """Build the Prompts tab with one QTextEdit per LLM role."""
        _roles = [
            ("Translator",  "translator_system_prompt", _SYSTEM_PROMPT,    "_translator_prompt_edit"),
            ("Descriptor",  "descriptor_system_prompt", _DESCRIBE_SYSTEM,  "_descriptor_prompt_edit"),
            ("Summarizer",  "summarizer_system_prompt", _SUMMARIZE_SYSTEM, "_summarizer_prompt_edit"),
        ]
        for display_name, cfg_attr, default_text, widget_attr in _roles:
            hdr = QHBoxLayout()
            lbl = QLabel(f"<b>{display_name}</b>")
            hdr.addWidget(lbl)
            hdr.addStretch()
            reset_btn = QPushButton("Reset to default")
            reset_btn.setFixedHeight(24)
            hdr.addWidget(reset_btn)
            parent.addLayout(hdr)

            edit = QTextEdit()
            edit.setPlainText(getattr(self._cfg, cfg_attr) or default_text)
            edit.setMinimumHeight(80)
            edit.setAcceptRichText(False)
            edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            parent.addWidget(edit, 1)   # stretch=1 → all three share vertical space equally
            setattr(self, widget_attr, edit)

            reset_btn.clicked.connect(
                lambda checked, e=edit, d=default_text: e.setPlainText(d)
            )

    def _build_buttons(self, parent: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.addStretch()

        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(_BTN_ACTION)
        cancel.clicked.connect(self._on_cancel)
        row.addWidget(cancel)

        save = QPushButton("Save")
        save.setStyleSheet(_BTN_SAVE)
        save.clicked.connect(self._on_save)
        row.addWidget(save)

        parent.addLayout(row)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _refresh_swatch(btn: QPushButton, color: list[int]) -> None:
        r, g, b, a = color
        btn.setStyleSheet(_BTN_SWATCH.format(r=r, g=g, b=b, a=a))

    def _pick_color(self, btn: QPushButton, setter) -> None:
        """Open a QColorDialog and call setter with [r,g,b,a] if accepted."""
        if setter == self._set_bg_color:
            current = list(self._cfg.overlay.bg_color)
        elif setter == self._set_jp_color:
            current = list(self._cfg.overlay.jp_color)
        else:
            current = list(self._cfg.overlay.en_color)

        r, g, b, a = current
        color = QColorDialog.getColor(
            QColor(r, g, b, a),
            self,
            "Pick Color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if color.isValid():
            new = [color.red(), color.green(), color.blue(), color.alpha()]
            setter(new)
            self._refresh_swatch(btn, new)

    # Font setter — mutate cfg and push live to overlay
    def _on_font_changed(self, attr: str, value) -> None:
        setattr(self._cfg.overlay, attr, value)
        self._overlay.apply_appearance(self._cfg.overlay)

    # Color setters — mutate cfg and push live to overlay
    def _set_bg_color(self, c: list[int]) -> None:
        self._cfg.overlay.bg_color = c
        self._overlay.apply_appearance(self._cfg.overlay)

    def _set_jp_color(self, c: list[int]) -> None:
        self._cfg.overlay.jp_color = c
        self._overlay.apply_appearance(self._cfg.overlay)

    def _set_en_color(self, c: list[int]) -> None:
        self._cfg.overlay.en_color = c
        self._overlay.apply_appearance(self._cfg.overlay)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_opacity_changed(self, value: int) -> None:
        self._opacity_lbl.setText(f"{value}%")
        self._cfg.overlay.bg_color[3] = int(value / 100 * 255)
        self._refresh_swatch(self._bg_btn, self._cfg.overlay.bg_color)
        self._overlay.apply_appearance(self._cfg.overlay)

    def _on_save(self) -> None:
        # API keys
        self._cfg.anthropic_api_key = self._anthropic_key_edit.text().strip()
        self._cfg.openai_api_key    = self._openai_key_edit.text().strip()
        self._cfg.google_api_key    = self._google_key_edit.text().strip()
        self._cfg.ollama_base_url   = self._ollama_url_edit.text().strip()

        # Per-role provider + model + thinking + max tokens
        for role_key, provider_attr, model_attr, thinking_attr, tokens_attr in [
            ("translator", "translator_provider", "translator_model", "translator_ollama_thinking", "translator_max_tokens"),
            ("descriptor", "descriptor_provider", "descriptor_model", "descriptor_ollama_thinking", "descriptor_max_tokens"),
            ("summarizer", "summarizer_provider", "summarizer_model", "summarizer_ollama_thinking", "summarizer_max_tokens"),
        ]:
            combo_text = self._role_combos[role_key].currentText()
            internal   = _PROVIDER_KEY_MAP.get(combo_text, "anthropic")
            stack      = self._role_model_stacks[role_key]
            if stack.currentIndex() == 1:
                model_val = self._role_model_edits[role_key].text().strip()
            else:
                model_val = self._role_model_combos[role_key].currentText()
                if not model_val or model_val == "Other\u2026":
                    model_val = ""
            if not model_val:
                model_val = ROLE_PROVIDER_DEFAULTS.get(role_key, {}).get(internal, "")
            setattr(self._cfg, provider_attr, internal)
            setattr(self._cfg, model_attr,    model_val)
            setattr(self._cfg, thinking_attr, self._role_think_checks[role_key].isChecked())
            setattr(self._cfg, tokens_attr,   self._role_max_tokens_spins[role_key].value())

        # Context settings
        self._cfg.clipboard_enabled        = self._clipboard_check.isChecked()
        self._cfg.screenshot_enabled       = self._screenshot_check.isChecked()
        self._cfg.context_summarize_tokens = self._summ_spin.value()
        self._cfg.context_min_recent_lines = self._lines_spin.value()

        # Scene change threshold (apply immediately to screenshot service)
        _pct = _slider_to_threshold(self._scene_thresh_slider.value())
        self._cfg.scene_change_threshold = _pct
        if self._screenshot_service is not None:
            self._screenshot_service.threshold = _pct

        # System prompts (empty string = use built-in default)
        self._cfg.translator_system_prompt = self._translator_prompt_edit.toPlainText().strip()
        self._cfg.descriptor_system_prompt = self._descriptor_prompt_edit.toPlainText().strip()
        self._cfg.summarizer_system_prompt = self._summarizer_prompt_edit.toPlainText().strip()

        # Appearance is already live in cfg.overlay — write everything to disk
        self._save_fn(self._cfg)

        # Update snapshot so a subsequent Cancel doesn't revert past this save
        self._orig_overlay = copy.deepcopy(self._cfg.overlay)
        self.accept()

    def _on_cancel(self) -> None:
        # Restore appearance to the state at dialog open
        self._cfg.overlay = copy.deepcopy(self._orig_overlay)
        self._overlay.apply_appearance(self._cfg.overlay)
        self.reject()
