from __future__ import annotations
from typing import Any, Callable, cast

from PyQt5.QtWidgets import (
    QAction,
    QSlider,
    QWidget,
    QPlainTextEdit,
    QLabel,
    QLineEdit,
    QMenu,
    QSpinBox,
    QToolButton,
    QComboBox,
    QHBoxLayout,
    QVBoxLayout,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QWidgetAction,
    QCheckBox,
    QGridLayout,
    QPushButton,
    QFrame,
    QScrollArea,
    QTabWidget,               # ADDED
)
from PyQt5.QtGui import (
    QDesktopServices,
    QGuiApplication,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPalette,
    QTextCursor,
    QPainter,
    QIcon,
    QPaintEvent,
    QCursor,
    QKeySequence,
)
from PyQt5.QtCore import QObject, Qt, QMetaObject, QSize, pyqtSignal, QEvent, QUrl
from krita import Krita

from ..style import Style, Styles
from ..root import root
from ..client import filter_supported_styles, resolve_arch
from ..properties import Binding, Bind, bind, bind_combo
from ..jobs import JobState, JobKind
from ..model import Model, Workspace, SamplingQuality, ProgressKind, ErrorKind, Error, no_error
from ..text import edit_attention, select_on_cursor_pos
from ..localization import translate as _
from ..util import ensure
from ..workflow import apply_strength, snap_to_percent
from ..settings import settings
from .autocomplete import PromptAutoComplete
from .theme import SignalBlocker
from . import actions, theme


class QueuePopup(QMenu):
    _model: Model
    _connections: list[QMetaObject.Connection]

    def __init__(self, supports_batch=True, parent: QWidget | None = None):
        super().__init__(parent)
        self._connections = []

        palette = self.palette()
        self.setObjectName("QueuePopup")
        self.setStyleSheet(
            f"""
            QWidget#QueuePopup {{
                background-color: {palette.window().color().name()}; 
                border: 1px solid {palette.dark().color().name()};
            }}"""
        )

        self._layout = QGridLayout()
        self.setLayout(self._layout)

        batch_label = QLabel(_("Batches"), self)
        batch_label.setVisible(supports_batch)
        self._layout.addWidget(batch_label, 0, 0)
        batch_layout = QHBoxLayout()
        self._batch_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._batch_slider.setMinimum(1)
        self._batch_slider.setMaximum(10)
        self._batch_slider.setSingleStep(1)
        self._batch_slider.setPageStep(1)
        self._batch_slider.setVisible(supports_batch)
        self._batch_slider.setToolTip(_("Number of jobs to enqueue at once"))
        self._batch_label = QLabel("1", self)
        self._batch_label.setVisible(supports_batch)
        batch_layout.addWidget(self._batch_slider)
        batch_layout.addWidget(self._batch_label)
        self._layout.addLayout(batch_layout, 0, 1)

        self._seed_label = QLabel(_("Seed"), self)
        self._layout.addWidget(self._seed_label, 1, 0)
        self._seed_input = QSpinBox(self)
        self._seed_check = QCheckBox(self)
        self._seed_check.setText(_("Fixed"))
        self._seed_input.setMinimum(0)
        self._seed_input.setMaximum(2**31 - 1)
        self._seed_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._seed_input.setToolTip(
            _(
                "The seed controls the random part of the output. A fixed seed value will always produce the same result for the same inputs."
            )
        )
        self._randomize_seed = QToolButton(self)
        self._randomize_seed.setIcon(theme.icon("random"))
        seed_layout = QHBoxLayout()
        seed_layout.addWidget(self._seed_check)
        seed_layout.addWidget(self._seed_input)
        seed_layout.addWidget(self._randomize_seed)
        self._layout.addLayout(seed_layout, 1, 1)

        enqueue_label = QLabel(_("Enqueue"), self)
        self._queue_front_combo = QComboBox(self)
        self._queue_front_combo.addItem(_("in Front (new jobs first)"), True)
        self._queue_front_combo.addItem(_("at the Back"), False)
        self._layout.addWidget(enqueue_label, 2, 0)
        self._layout.addWidget(self._queue_front_combo, 2, 1)

        cancel_label = QLabel(_("Cancel"), self)
        self._layout.addWidget(cancel_label, 3, 0)
        self._cancel_active = self._create_cancel_button(_("Active"), actions.cancel_active)
        self._cancel_queued = self._create_cancel_button(_("Queued"), actions.cancel_queued)
        self._cancel_all = self._create_cancel_button(_("All"), actions.cancel_all)
        cancel_layout = QHBoxLayout()
        cancel_layout.addWidget(self._cancel_active)
        cancel_layout.addWidget(self._cancel_queued)
        cancel_layout.addWidget(self._cancel_all)
        self._layout.addLayout(cancel_layout, 3, 1)

        self._model = root.active_model

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        Binding.disconnect_all(self._connections)
        self._model = model
        self._randomize_seed.setEnabled(self._model.fixed_seed)
        self._seed_input.setEnabled(self._model.fixed_seed)
        self._batch_label.setText(str(self._model.batch_count))
        self._connections = [
            bind(self._model, "batch_count", self._batch_slider, "value"),
            model.batch_count_changed.connect(lambda v: self._batch_label.setText(str(v))),
            bind(self._model, "seed", self._seed_input, "value"),
            bind(self._model, "fixed_seed", self._seed_check, "checked", Bind.one_way),
            self._seed_check.toggled.connect(lambda v: setattr(self._model, "fixed_seed", v)),
            self._model.fixed_seed_changed.connect(self._seed_input.setEnabled),
            self._model.fixed_seed_changed.connect(self._randomize_seed.setEnabled),
            self._randomize_seed.clicked.connect(self._model.generate_seed),
            bind_combo(self._model, "queue_front", self._queue_front_combo),
            model.jobs.count_changed.connect(self._update_cancel_buttons),
        ]

    def _create_cancel_button(self, name: str, action: Callable[[], None]):
        button = QToolButton(self)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setText(name)
        button.setIcon(theme.icon("cancel"))
        button.setEnabled(False)
        button.clicked.connect(action)
        return button

    def _update_cancel_buttons(self):
        has_active = self._model.jobs.any_executing()
        has_queued = self._model.jobs.count(JobState.queued) > 0
        self._cancel_active.setEnabled(has_active)
        self._cancel_queued.setEnabled(has_queued)
        self._cancel_all.setEnabled(has_active or has_queued)

    def mouseReleaseEvent(self, a0: QMouseEvent | None) -> None:
        if parent := cast(QWidget, self.parent()):
            parent.close()
        return super().mouseReleaseEvent(a0)


class QueueButton(QToolButton):

    def __init__(self, supports_batch=True, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = root.active_model
        self._connect_model()

        self._popup = QueuePopup(supports_batch)
        popup_action = QWidgetAction(self)
        popup_action.setDefaultWidget(self._popup)
        self.addAction(popup_action)

        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._update()

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model != model:
            Binding.disconnect_all(self._connections)
            self._model = model
            self._popup.model = model
            self._connect_model()

    def _connect_model(self):
        self._connections = [
            self._model.jobs.count_changed.connect(self._update),
            self._model.progress_kind_changed.connect(self._update),
        ]

    def _update(self):
        count = self._model.jobs.count(JobState.queued)
        queued_msg = _("{count} jobs queued.", count=count)
        cancel_msg = _("Click to cancel.")

        if self._model.progress_kind is ProgressKind.upload:
            self.setIcon(theme.icon("queue-upload"))
            self.setToolTip(_("Uploading models.") + f" {queued_msg} {cancel_msg}")
            count += 1
        elif self._model.jobs.any_executing():
            self.setIcon(theme.icon("queue-active"))
            if count > 0:
                self.setToolTip(_("Generating image.") + f" {queued_msg} {cancel_msg}")
            else:
                self.setToolTip(_("Generating image.") + f" {cancel_msg}")
            count += 1
        else:
            self.setIcon(theme.icon("queue-inactive"))
            self.setToolTip(_("Idle."))
        self.setText(f"{count} ")

    def sizeHint(self) -> QSize:
        original = super().sizeHint()
        width = original.height() * 0.75 + self.fontMetrics().width(" 99 ") + 20
        return QSize(int(width), original.height())

    def paintEvent(self, a0):
        _paint_tool_drop_down(self, self.text())


class StyleSelectWidget(QWidget):
    _value: Style
    _styles: list[Style]

    value_changed = pyqtSignal(Style)
    quality_changed = pyqtSignal(SamplingQuality)

    def __init__(self, parent: QWidget | None, show_quality=False):
        super().__init__(parent)
        self._value = Styles.list().default

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self._combo = QComboBox(self)
        self.update_styles()
        self._combo.currentIndexChanged.connect(self.change_style)
        layout.addWidget(self._combo, 3)

        if show_quality:
            self._quality_combo = QComboBox(self)
            self._quality_combo.addItem(_("Fast"), SamplingQuality.fast.value)
            self._quality_combo.addItem(_("Quality"), SamplingQuality.quality.value)
            self._quality_combo.currentIndexChanged.connect(self.change_quality)
            layout.addWidget(self._quality_combo, 1)

        settings = QToolButton(self)
        settings.setIcon(theme.icon("settings"))
        settings.setAutoRaise(True)
        settings.clicked.connect(self.show_settings)
        layout.addWidget(settings)

        Styles.list().changed.connect(self.update_styles)
        Styles.list().name_changed.connect(self.update_styles)
        root.connection.state_changed.connect(self.update_styles)

    def update_styles(self):
        comfy = root.connection.client_if_connected
        self._styles = filter_supported_styles(Styles.list().filtered(), comfy)
        with SignalBlocker(self._combo):
            self._combo.clear()
            for style in self._styles:
                icon = theme.checkpoint_icon(resolve_arch(style, comfy))
                self._combo.addItem(icon, style.name, style.filename)
            if self._value in self._styles:
                self._combo.setCurrentText(self._value.name)
            elif len(self._styles) > 0:
                self._value = self._styles[0]
                self._combo.setCurrentIndex(0)

    def change_style(self):
        style = self._styles[self._combo.currentIndex()]
        if style != self._value:
            self._value = style
            self.value_changed.emit(style)

    def change_quality(self):
        quality = SamplingQuality(self._quality_combo.currentData())
        self.quality_changed.emit(quality)

    def show_settings(self):
        from .settings import SettingsDialog

        SettingsDialog.instance().show(self._value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, style: Style):
        if style != self._value:
            self._value = style
            self._combo.setCurrentText(style.name)


def handle_weight_adjustment(
    self: MultiLineTextPromptWidget | SingleLineTextPromptWidget, event: QKeyEvent
):
    """Handles Ctrl + (arrow key up / arrow key down) attention weight adjustment."""
    if event.key() in [Qt.Key.Key_Up, Qt.Key.Key_Down] and (event.modifiers() & Qt.Modifier.CTRL):
        if self.hasSelectedText():
            start = self.selectionStart()
            end = self.selectionEnd()
        else:
            start, end = select_on_cursor_pos(self.text(), self.cursorPosition())

        text = self.text()
        target_text = text[start:end]
        text_after_edit = edit_attention(target_text, event.key() == Qt.Key.Key_Up)
        self.setText(text[:start] + text_after_edit + text[end:])
        if isinstance(self, MultiLineTextPromptWidget):
            self.setSelection(start, start + len(text_after_edit))
        else:
            # Note: setSelection has some wield bug in `SingleLineTextPromptWidget`
            # that the end range will be set to end of text. So set cursor instead
            # as compromise.
            self.setCursorPosition(start + len(text_after_edit) - 2)


class MultiLineTextPromptWidget(QPlainTextEdit):
    activated = pyqtSignal()

    _line_count = 2

    def __init__(self, parent: QWidget | None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setFrameStyle(QFrame.Shape.NoFrame)
        self.line_count = 2

        self._completer = PromptAutoComplete(self)
        self.textChanged.connect(self._completer.check_completion)

    def event(self, e: QEvent | None):
        assert e is not None
        # Ctrl+Backspace should be handled by QPlainTextEdit, not Krita.
        if e.type() == QEvent.Type.ShortcutOverride:
            assert isinstance(e, QKeyEvent)
            if e.matches(QKeySequence.DeleteStartOfWord):
                e.accept()
        return super().event(e)

    def keyPressEvent(self, e: QKeyEvent | None):
        assert e is not None
        if self._completer.is_active and e.key() in PromptAutoComplete.action_keys:
            e.ignore()
            return

        handle_weight_adjustment(self, e)

        if e.key() == Qt.Key.Key_Return and e.modifiers() == Qt.KeyboardModifier.ShiftModifier:
            self.activated.emit()
        else:
            super().keyPressEvent(e)

    @property
    def line_count(self):
        return self._line_count

    @line_count.setter
    def line_count(self, value: int):
        self._line_count = value
        fm = QFontMetrics(ensure(self.document()).defaultFont())
        self.setFixedHeight(fm.lineSpacing() * value + 8)

    def hasSelectedText(self) -> bool:
        return self.textCursor().hasSelection()

    def selectionStart(self) -> int:
        return self.textCursor().selectionStart()

    def selectionEnd(self) -> int:
        return self.textCursor().selectionEnd()

    def cursorPosition(self) -> int:
        return self.textCursor().position()

    def setCursorPosition(self, pos: int):
        cursor = self.textCursor()
        cursor.setPosition(pos)
        self.setTextCursor(cursor)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str):
        self.setPlainText(text)

    def setSelection(self, start: int, end: int):
        new_cursor = self.textCursor()
        new_cursor.setPosition(min(end, len(self.text())))
        new_cursor.setPosition(min(start, len(self.text())), QTextCursor.KeepAnchor)
        self.setTextCursor(new_cursor)


class SingleLineTextPromptWidget(QLineEdit):

    _completer: PromptAutoComplete

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._completer = PromptAutoComplete(self)
        self.textChanged.connect(self._completer.check_completion)
        self.setFrame(False)
        self.setStyleSheet(f"QLineEdit {{ background: transparent; }}")

    def keyPressEvent(self, a0: QKeyEvent | None):
        assert a0 is not None
        handle_weight_adjustment(self, a0)
        super().keyPressEvent(a0)


class TextPromptWidget(QFrame):
    """Wraps a single or multi-line text widget, with ability to switch between them.
    Using QPlainTextEdit set to a single line doesn't work properly because it still
    scrolls to the next line when eg. selecting and then looks like it's empty."""

    activated = pyqtSignal()
    text_changed = pyqtSignal(str)

    _line_count = 2
    _is_negative = False

    def __init__(self, line_count=2, is_negative=False, parent=None):
        super().__init__(parent)
        self._line_count = line_count
        self._is_negative = is_negative
        self._layout = QVBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._layout)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._multi = MultiLineTextPromptWidget(self)
        self._multi.line_count = self._line_count
        self._multi.activated.connect(self.notify_activated)
        self._multi.textChanged.connect(self.notify_text_changed)
        self._multi.setVisible(self._line_count > 1)

        self._single = SingleLineTextPromptWidget(self)
        self._single.textChanged.connect(self.notify_text_changed)
        self._single.returnPressed.connect(self.notify_activated)
        self._single.setVisible(self._line_count == 1)

        self._layout.addWidget(self._multi)
        self._layout.addWidget(self._single)

        palette: QPalette = self._multi.palette()
        self._base_color = palette.color(QPalette.ColorRole.Base)
        self.is_negative = self._is_negative

    def notify_text_changed(self):
        self.text_changed.emit(self.text)

    def notify_activated(self):
        self.activated.emit()

    @property
    def text(self):
        return self._multi.text() if self._line_count > 1 else self._single.text()

    @text.setter
    def text(self, value: str):
        if value == self.text:
            return
        widget = self._multi if self._line_count > 1 else self._single
        with SignalBlocker(widget):  # avoid auto-completion on non-user input
            widget.setText(value)

    @property
    def line_count(self):
        return self._line_count

    @line_count.setter
    def line_count(self, value: int):
        text = self.text
        self._line_count = value
        self.text = text
        self._multi.setVisible(self._line_count > 1)
        self._single.setVisible(self._line_count == 1)
        if self._line_count > 1:
            self._multi.line_count = self._line_count

    @property
    def is_negative(self):
        return self._is_negative

    @is_negative.setter
    def is_negative(self, value: bool):
        self._is_negative = value
        for w in (self._multi, self._single):
            if not value:
                w.setPlaceholderText(_("Describe the content you want to see, or leave empty."))
            else:
                w.setPlaceholderText(_("Describe content you want to avoid."))

        if value:
            self.setContentsMargins(0, 2, 0, 2)
            self.setFrameStyle(QFrame.Shape.StyledPanel)
            self.setStyleSheet(f"QFrame {{ background: rgba(255, 0, 0, 15); }}")
        else:
            self.setFrameStyle(QFrame.Shape.NoFrame)

    @property
    def has_focus(self):
        return self._multi.hasFocus() or self._single.hasFocus()

    @has_focus.setter
    def has_focus(self, value: bool):
        if value:
            if self._line_count > 1:
                self._multi.setFocus()
            else:
                self._single.setFocus()

    def install_event_filter(self, obj: QObject):
        self._multi.installEventFilter(obj)
        self._single.installEventFilter(obj)

    def move_cursor_to_end(self):
        if self._line_count > 1:
            cursor = self._multi.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._multi.setTextCursor(cursor)
        else:
            self._single.setCursorPosition(len(self._single.text()))


class StrengthSnapping:
    model: Model

    def __init__(self, model: Model):
        self.model = model

    def get_steps(self) -> tuple[int, int]:
        is_live = self.model.workspace is Workspace.live
        if self.model.workspace is Workspace.animation:
            is_live = self.model.animation.sampling_quality is SamplingQuality.fast
        return self.model.style.get_steps(is_live=is_live)

    def nearest_percent(self, value: int) -> int | None:
        _, max_steps = self.get_steps()
        steps, start_at_step = self.apply_strength(value)
        return snap_to_percent(steps, start_at_step, max_steps=max_steps)

    def apply_strength(self, value: int) -> tuple[int, int]:
        min_steps, max_steps = self.get_steps()
        strength = value / 100
        return apply_strength(strength, steps=max_steps, min_steps=min_steps)


# SpinBox variant that allows manually entering strength values,
# but snaps to model_steps on step actions (scrolling, arrows, arrow keys).
class StrengthSpinBox(QSpinBox):
    snapping: StrengthSnapping | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapping = None
        # for manual input
        self.setMinimum(1)
        self.setMaximum(100)

    def stepBy(self, steps):
        value = max(self.minimum(), min(self.maximum(), self.value() + steps))
        if self.snapping is not None:
            # keep going until we hit a new snap point
            current_point = self.nearest_snap_point(self.value())
            while self.nearest_snap_point(value) == current_point and value > 1:
                value += 1 if steps > 0 else -1
            value = self.nearest_snap_point(value)
        self.setValue(value)

    def nearest_snap_point(self, value: int) -> int:
        assert self.snapping
        return self.snapping.nearest_percent(value) or (int(value / 5) * 5)


class StrengthWidget(QWidget):
    _model: Model | None = None
    _value: int = 100

    value_changed = pyqtSignal(float)

    def __init__(self, slider_range: tuple[int, int] = (1, 100), parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout()
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._layout)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(slider_range[0])
        self._slider.setMaximum(slider_range[1])
        self._slider.setValue(self._value)
        self._slider.setSingleStep(5)
        self._slider.valueChanged.connect(self.slider_changed)

        self._input = StrengthSpinBox(self)
        self._input.setValue(self._value)
        self._input.setPrefix(_("Strength") + ": ")
        self._input.setSuffix("%")
        self._input.valueChanged.connect(self.notify_changed)

        settings.changed.connect(self.update_suffix)

        self._layout.addWidget(self._slider)
        self._layout.addWidget(self._input)

    def slider_changed(self, value: int):
        if self._input.snapping is not None:
            value = self._input.snapping.nearest_percent(value) or value
        self.notify_changed(value)

    def notify_changed(self, value: int):
        if self._update_value(value):
            self.value_changed.emit(self.value)

    def _update_value(self, value: int):
        with SignalBlocker(self._slider), SignalBlocker(self._input):
            self._slider.setValue(value)
            self._input.setValue(value)
        if value != self._value:
            self._value = value
            self.update_suffix()
            return True
        return False

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model:
            self._model.style_changed.disconnect(self.update_suffix)
            self._model.animation.sampling_quality_changed.disconnect(self.update_suffix)
        self._model = model
        self._model.style_changed.connect(self.update_suffix)
        self._model.animation.sampling_quality_changed.connect(self.update_suffix)
        self._input.snapping = StrengthSnapping(self._model)
        self.update_suffix()

    @property
    def value(self):
        return self._value / 100

    @value.setter
    def value(self, value: float):
        if value == self.value:
            return
        self._update_value(round(value * 100))

    def update_suffix(self):
        if not self._input.snapping or not settings.show_steps:
            self._input.setSuffix("%")
            return

        steps, start_at_step = self._input.snapping.apply_strength(self._value)
        self._input.setSuffix(f"% ({steps - start_at_step}/{steps})")


class WorkspaceSelectWidget(QToolButton):
    _icons = {
        Workspace.generation: theme.icon("workspace-generation"),
        Workspace.upscaling: theme.icon("workspace-upscaling"),
        Workspace.live: theme.icon("workspace-live"),
        Workspace.animation: theme.icon("workspace-animation"),
        Workspace.custom: theme.icon("workspace-custom"),
    }

    _value = Workspace.generation

    def __init__(self, parent):
        super().__init__(parent)

        menu = QMenu(self)
        menu.addAction(self._create_action(_("Generate"), Workspace.generation))
        menu.addAction(self._create_action(_("Upscale"), Workspace.upscaling))
        menu.addAction(self._create_action(_("Live"), Workspace.live))
        menu.addAction(self._create_action(_("Animation"), Workspace.animation))
        menu.addAction(self._create_action(_("Graph"), Workspace.custom))

        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setMenu(menu)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolTip(
            _("Switch between workspaces: image generation, upscaling, live preview and animation.")
        )
        self.setMinimumWidth(int(self.sizeHint().width() * 1.6))
        self.value = Workspace.generation

    def paintEvent(self, a0):
        _paint_tool_drop_down(self)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, workspace: Workspace):
        self._value = workspace
        self.setIcon(self._icons[workspace])

    def _create_action(self, name: str, workspace: Workspace):
        action = QAction(name, self)
        action.setIcon(self._icons[workspace])
        action.setIconVisibleInMenu(True)
        action.triggered.connect(actions.set_workspace(workspace))
        return action


class GenerateButton(QPushButton):
    model: Model
    _operation: str
    _kind: JobKind
    _cost: int = 0
    _cost_icon: QIcon

    def __init__(self, kind: JobKind, parent: QWidget):
        super().__init__(parent)
        self.model = root.active_model
        self._operation = _("Generate")
        self._kind = kind
        self._cost_icon = theme.icon("interstice")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    @property
    def operation(self):
        return self._operation

    @operation.setter
    def operation(self, value: str):
        self._operation = value
        self.update()

    def minimumSizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.width(self._operation) + 40, 12 + int(1.3 * fm.height()))

    def enterEvent(self, a0: QEvent | None):
        if client := root.connection.client_if_connected:
            if client.user:
                self._cost = self.model.estimate_cost(self._kind)

    def leaveEvent(self, a0: QEvent | None):
        self._cost = 0

    def paintEvent(self, a0: QPaintEvent | None) -> None:
        opt = QStyleOption()
        opt.initFrom(self)
        opt.state |= QStyle.StateFlag.State_Sunken if self.isDown() else 0
        painter = QPainter(self)
        fm = self.fontMetrics()
        style = ensure(self.style())
        rect = self.rect()
        pixmap = self.icon().pixmap(int(fm.height() * 1.3))
        is_hover = int(opt.state) & QStyle.StateFlag.State_MouseOver
        element = QStyle.PrimitiveElement.PE_PanelButtonCommand
        vcenter = Qt.AlignmentFlag.AlignVCenter
        content_width = fm.width(self._operation) + 5 + pixmap.width()
        content_rect = rect.adjusted(int(0.5 * (rect.width() - content_width)), 0, 0, 0)
        style.drawPrimitive(element, opt, painter, self)
        style.drawItemPixmap(painter, content_rect, vcenter, pixmap)
        content_rect = content_rect.adjusted(pixmap.width() + 5, 0, 0, 0)
        style.drawItemText(painter, content_rect, vcenter, self.palette(), True, self._operation)

        if is_hover and self._cost > 0:
            cost_width = fm.width(str(self._cost))
            pixmap = self._cost_icon.pixmap(fm.height())
            cost_rect = rect.adjusted(rect.width() - pixmap.width() - cost_width - 16, 0, 0, 0)
            painter.setOpacity(0.3)
            painter.drawLine(
                cost_rect.left(), cost_rect.top() + 6, cost_rect.left(), cost_rect.bottom() - 6
            )
            painter.setOpacity(0.7)
            cost_rect = cost_rect.adjusted(6, 0, 0, 0)
            style.drawItemText(painter, cost_rect, vcenter, self.palette(), True, str(self._cost))
            cost_rect = cost_rect.adjusted(cost_width + 4, 0, 0, 0)
            style.drawItemPixmap(painter, cost_rect, vcenter, pixmap)


class ErrorBox(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._error = no_error
        self._original_error = ""

        self.setObjectName("errorBox")
        self.setFrameStyle(QFrame.Shape.StyledPanel)

        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setOpenExternalLinks(True)
        self._label.setTextFormat(Qt.TextFormat.RichText)

        self._copy_button = QToolButton(self)
        self._copy_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._copy_button.setIcon(Krita.instance().icon("edit-copy"))
        self._copy_button.setToolTip(_("Copy error message to clipboard"))
        self._copy_button.setAutoRaise(True)
        self._copy_button.clicked.connect(self._copy_error)

        self._recharge_button = QToolButton(self)
        self._recharge_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._recharge_button.setText(_("Charge"))
        self._recharge_button.setIcon(theme.icon("interstice"))
        self._recharge_button.clicked.connect(self._recharge)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._label)
        layout.addWidget(self._copy_button)
        layout.addWidget(self._recharge_button)

        self.reset()

    def reset(self, color: str = theme.red):
        self._copy_button.setVisible(False)
        self._recharge_button.setVisible(False)
        self._label.setStyleSheet(f"color: {color};")
        if color == theme.red:
            self.setStyleSheet("QFrame#errorBox { border: 1px solid #a01020; }")
        else:
            self.setStyleSheet(None)
        self.hide()

    @property
    def error(self):
        return self._error

    @error.setter
    def error(self, error: Error):
        self.reset()
        self._error = error
        self._original_error = error.message if error else ""
        if error.kind is ErrorKind.insufficient_funds:
            self._show_payment_error(error.data)
        elif error:
            self._show_error(error.message)

    def _show_error(self, text: str):
        if text.count("\n") > 3:
            lines = text.split("\n")
            n = 1
            text = lines[-n]
            while n < len(lines) and text.strip() == "":
                n += 1
                text = lines[-n]
        if len(text) > 60 * 3:
            text = text[: 60 * 2] + " [...] " + text[-60:]
        self._label.setText(text)
        if text != self._original_error:
            self._label.setToolTip(self._original_error)
        self._copy_button.setVisible(True)
        self.show()

    def _show_payment_error(self, data: dict[str, Any] | None):
        self.reset(theme.yellow)
        message = "Insufficient funds"
        if data:
            message = _(
                "Insufficient funds - generation would cost {cost} tokens. Remaining tokens: {tokens}",
                cost=data["cost"],
                tokens=data["credits"],
            )
        self._label.setText(message)
        self._recharge_button.setVisible(True)
        self.show()

    def _copy_error(self):
        if clipboard := QGuiApplication.clipboard():
            clipboard.setText(self._original_error)

    def _recharge(self):
        QDesktopServices.openUrl(QUrl("https://www.interstice.cloud/user"))


def create_wide_tool_button(icon_name: str, text: str, parent=None):
    button = QToolButton(parent)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    button.setIcon(theme.icon(icon_name))
    button.setToolTip(text)
    button.setAutoRaise(True)
    icon_height = button.iconSize().height()
    button.setIconSize(QSize(int(icon_height * 1.25), icon_height))
    return button


def create_framed_label(text: str, parent=None):
    frame = QFrame(parent)
    frame.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Plain)
    label = QLabel(parent=frame)
    label.setText(text)
    frame_layout = QHBoxLayout()
    frame_layout.setContentsMargins(4, 2, 4, 2)
    frame_layout.addWidget(label)
    frame.setLayout(frame_layout)
    return frame, label


def _paint_tool_drop_down(widget: QToolButton, text: str | None = None):
    opt = QStyleOption()
    opt.initFrom(widget)
    painter = QPainter(widget)
    style = ensure(widget.style())
    rect = widget.rect()
    pixmap = widget.icon().pixmap(int(rect.height() * 0.75))
    element = QStyle.PrimitiveElement.PE_Widget
    if int(opt.state) & QStyle.StateFlag.State_MouseOver:
        element = QStyle.PrimitiveElement.PE_PanelButtonCommand
    style.drawPrimitive(element, opt, painter, widget)
    style.drawItemPixmap(
        painter,
        rect.adjusted(4, 0, 0, 0),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        pixmap,
    )
    if text:
        text_rect = rect.adjusted(pixmap.width() + 4, 0, 0, 0)
        style.drawItemText(
            painter, text_rect, Qt.AlignmentFlag.AlignVCenter, widget.palette(), True, text
        )
    painter.translate(int(0.5 * rect.width() - 10), 0)
    style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorArrowDown, opt, painter)

# ----------------------------------------------------------------------------
# ADDED/CHANGED: PROMPT HISTORY LOGIC
# ----------------------------------------------------------------------------

import os
import json
from PyQt5.QtCore import QStandardPaths


PROMPT_HISTORY_FILENAME = "prompt_history.json"


def prompt_history_file_path() -> str:
    """
    Return a path in the user's writable config/data directory for storing prompt history.
    Feel free to change to your plugin's custom location as needed.
    """
    dirpath = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)
    return os.path.join(dirpath, PROMPT_HISTORY_FILENAME)


class PromptHistoryManager:
    """
    Loads/saves the user’s prompt history and favorites from a JSON file. 
    Data structure in JSON is:
    {
      "prompts": [
        {"name": "...", "text": "..."},
        ...
      ],
      "favorites": [
        {"name": "...", "text": "..."},
        ...
      ]
    }
    """
    def __init__(self):
        self._data: dict = {"prompts": [], "favorites": []}
        self.load()

    def load(self):
        path = prompt_history_file_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except:
                self._data = {"prompts": [], "favorites": []}
        else:
            self._data = {"prompts": [], "favorites": []}

    def save(self):
        path = prompt_history_file_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def prompts(self):
        return self._data["prompts"]

    def favorites(self):
        return self._data["favorites"]

    def add_prompt(self, name: str, text: str):
        self._data["prompts"].append({"name": name, "text": text})
        self.save()

    def remove_prompt(self, idx: int):
        if 0 <= idx < len(self._data["prompts"]):
            del self._data["prompts"][idx]
            self.save()

    def clear_prompts(self):
        self._data["prompts"] = []
        self.save()

    def add_favorite(self, name: str, text: str):
        self._data["favorites"].append({"name": name, "text": text})
        self.save()

    def remove_favorite(self, idx: int):
        if 0 <= idx < len(self._data["favorites"]):
            del self._data["favorites"][idx]
            self.save()

    def move_to_favorites(self, idx: int):
        """Favoriting an item from normal prompts: remove from prompts and add to favorites."""
        if 0 <= idx < len(self._data["prompts"]):
            item = self._data["prompts"][idx]
            # add to favorites
            self._data["favorites"].append(item)
            # remove from normal
            del self._data["prompts"][idx]
            self.save()

    def rename_prompt(self, idx: int, is_favorite: bool, new_name: str):
        """Rename the prompt's name in normal or favorite list."""
        if not is_favorite:
            if 0 <= idx < len(self._data["prompts"]):
                self._data["prompts"][idx]["name"] = new_name
                self.save()
        else:
            if 0 <= idx < len(self._data["favorites"]):
                self._data["favorites"][idx]["name"] = new_name
                self.save()


class PromptHistoryPopup(QFrame):
    def __init__(
        self,
        manager: PromptHistoryManager,
        on_use_prompt: Callable[[str], None],
        parent: QWidget | None = None
    ):
        super().__init__(parent, flags=Qt.Tool | Qt.FramelessWindowHint)
        self.manager = manager
        self.on_use_prompt = on_use_prompt  # <-- store the callback here

        # Make sure this stays open until clicked outside
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.StrongFocus)

        # ADDED: size control and scroll areas
        # You can tweak these defaults freely
        DEFAULT_POPUP_WIDTH = 500
        DEFAULT_POPUP_HEIGHT = 500
        self.setFixedSize(DEFAULT_POPUP_WIDTH, DEFAULT_POPUP_HEIGHT)

        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(5, 5, 5, 5)
        self.setLayout(self.layout_)

        self.tabs = QTabWidget(self)
        self.layout_.addWidget(self.tabs)

        # Create Prompts tab with scrollable area
        self.prompts_scroll = QScrollArea(self)
        self.prompts_scroll.setWidgetResizable(True)
        self.prompts_widget = QWidget(self)
        self.prompts_layout = QVBoxLayout(self.prompts_widget)
        self.prompts_widget.setLayout(self.prompts_layout)

        # "Delete All" button at top
        delete_all_btn = QPushButton(_("Delete All"), self.prompts_widget)
        delete_all_btn.clicked.connect(self._delete_all_prompts)
        self.prompts_layout.addWidget(delete_all_btn)

        self.prompts_list = QVBoxLayout()
        self.prompts_layout.addLayout(self.prompts_list)
        self.prompts_layout.addStretch()  # push items to top

        self.prompts_scroll.setWidget(self.prompts_widget)
        self.tabs.addTab(self.prompts_scroll, _("Prompts"))

        # Create Favorites tab with scrollable area
        self.favorites_scroll = QScrollArea(self)
        self.favorites_scroll.setWidgetResizable(True)
        self.favorites_widget = QWidget(self)
        self.favorites_layout = QVBoxLayout(self.favorites_widget)
        self.favorites_widget.setLayout(self.favorites_layout)

        self.favorites_list = QVBoxLayout()
        self.favorites_layout.addLayout(self.favorites_list)
        self.favorites_layout.addStretch()

        self.favorites_scroll.setWidget(self.favorites_widget)
        self.tabs.addTab(self.favorites_scroll, _("Favorites"))

        self._populate()

    def _populate(self):
        # Clear out old items from each layout:
        self._clear_layout(self.prompts_list)
        self._clear_layout(self.favorites_list)

        # Rebuild normal prompts
        for i, item in enumerate(self.manager.prompts()):
            w = self._make_prompt_item(i, item["name"], item["text"], is_favorite=False)
            self.prompts_list.addWidget(w)

        # Rebuild favorites
        for i, item in enumerate(self.manager.favorites()):
            w = self._make_prompt_item(i, item["name"], item["text"], is_favorite=True)
            self.favorites_list.addWidget(w)

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _make_prompt_item(self, idx: int, name: str, text: str, is_favorite: bool):
        frame = QFrame(self)
        frame.setFrameStyle(QFrame.StyledPanel | QFrame.Plain)
        f_layout = QVBoxLayout(frame)
        f_layout.setContentsMargins(5, 5, 5, 5)
        frame.setLayout(f_layout)

        # 1) Naming row
        naming_row = QHBoxLayout()
        naming_row.setSpacing(5)
        name_edit = QLineEdit(name, frame)
        name_edit.textChanged.connect(
            lambda new_name, i=idx, fav=is_favorite: self.manager.rename_prompt(i, fav, new_name)
        )
        naming_row.addWidget(QLabel(_("Name:"), frame))
        naming_row.addWidget(name_edit)
        f_layout.addLayout(naming_row)

        # 2) Prompt text label
        text_label = QLabel(f"“{text}”", frame)
        text_label.setWordWrap(True)
        text_label.setStyleSheet("color: #888;")
        f_layout.addWidget(text_label)

        # 3) Buttons row: Use, Favorite/Unfavorite, Delete
        buttons_row = QHBoxLayout()

        # ADDED: The "Use" button (put it before Favorite)
        use_btn = QPushButton(_("Use"), frame)
        use_btn.setToolTip(_("Copy this prompt to the main text box"))
        use_btn.clicked.connect(lambda _, i=idx, fav=is_favorite: self._on_use_clicked(i, fav))
        buttons_row.addWidget(use_btn)

        # Favorite/unfavorite
        fav_btn = QPushButton(self)
        if is_favorite:
            fav_btn.setText(_("Unfavorite"))
        else:
            fav_btn.setText(_("Favorite"))
        fav_btn.clicked.connect(lambda _, i=idx, fav=is_favorite: self._on_favorite_clicked(i, fav))
        buttons_row.addWidget(fav_btn)

        # Delete
        del_btn = QPushButton(_("Delete"), self)
        del_btn.clicked.connect(lambda _, i=idx, fav=is_favorite: self._on_delete_clicked(i, fav))
        buttons_row.addWidget(del_btn)

        f_layout.addLayout(buttons_row)
        return frame

    def _on_use_clicked(self, idx: int, is_favorite: bool):
        """
        Retrieve the prompt text from the manager and call `self.on_use_prompt(text)`
        to load it into the main prompt box.
        """
        if is_favorite:
            text = self.manager.favorites()[idx]["text"]
        else:
            text = self.manager.prompts()[idx]["text"]
        self.on_use_prompt(text)

    def _on_favorite_clicked(self, idx: int, is_favorite: bool):
        if is_favorite:
            self.manager.remove_favorite(idx)
        else:
            self.manager.move_to_favorites(idx)
        self._populate()

    def _on_delete_clicked(self, idx: int, is_favorite: bool):
        if is_favorite:
            self.manager.remove_favorite(idx)
        else:
            self.manager.remove_prompt(idx)
        self._populate()

    def _delete_all_prompts(self):
        self.manager.clear_prompts()
        self._populate()

    def focusOutEvent(self, event):
        """Close when clicking outside this popup."""
        super().focusOutEvent(event)
        global_pos = QCursor.pos()  # safe in PyQt5
        if not self.rect().contains(self.mapFromGlobal(global_pos)):
            self.close()

class PromptHistoryButton(QToolButton):
    def __init__(
        self, 
        model: Model, 
        region_prompt_widget: RegionPromptWidget,  # or any reference to your main prompt box
        parent: QWidget | None = None
    ):
        super().__init__(parent)
        self._model = model
        self._manager = PromptHistoryManager()
        self._region_prompt_widget = region_prompt_widget

        self.setText(_("History"))
        self.setToolTip(_("Open Prompt History"))
        self.setIcon(theme.icon("history"))
        self.setPopupMode(QToolButton.InstantPopup)

        self.clicked.connect(self._on_clicked)

    @property
    def manager(self):
        return self._manager

    def _on_clicked(self):
        # We'll pass a callback that sets the region prompt text:
        def use_prompt_callback(prompt_text: str):
            # For example, if RegionPromptWidget has a method:
            self._region_prompt_widget.model.regions.active_or_root.positive = prompt_text

        popup = PromptHistoryPopup(self._manager, on_use_prompt=use_prompt_callback, parent=self.parent())
        popup.adjustSize()  # in case you want to do it here

        # Position the popup near this button
        button_pos = self.mapToGlobal(self.rect().bottomLeft())
        popup.move(button_pos)
        popup.show()
        popup.activateWindow()
        popup.setFocus()

