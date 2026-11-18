"""Native setup for our bundled input engine; no upstream settings application."""
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from . import scroll


def disclosure(label, content):
    toggle = QToolButton()
    toggle.setText(label)
    toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    toggle.setArrowType(Qt.RightArrow)
    toggle.setCheckable(True)
    content.hide()

    def reveal(visible):
        toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        content.setVisible(visible)
        if not visible:
            content.window().adjustSize()

    toggle.toggled.connect(reveal)
    return toggle


class WheelSettings(QWidget):
    def __init__(self, window):
        super().__init__()
        self.engine = window.engine
        current = scroll.load(window.service.profile)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.controls = {}
        for label, first, second in [
            ('本机平滑', 'smooth_vertical', 'smooth_horizontal'),
            ('本机反向', 'reverse_vertical', 'reverse_horizontal'),
            ('跨屏反向', 'remote_reverse_vertical', 'remote_reverse_horizontal'),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addStretch()
            for key, title in [(first, '上下'), (second, '左右')]:
                check = QCheckBox(title)
                check.setAccessibleName(label + title)
                check.setChecked(current[key])
                self.controls[key] = check
                row.addWidget(check)
            box.addLayout(row)

        def number(target, key, title, suffix, step):
            row = QHBoxLayout()
            row.addWidget(QLabel(title))
            row.addStretch()
            spin = QSpinBox()
            spin.setAccessibleName(title)
            spin.setRange(*scroll.LIMITS[key])
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            spin.setValue(current[key])
            self.controls[key] = spin
            row.addWidget(spin)
            target.addLayout(row)

        number(box, 'speed', '速度', '%', 10)
        advanced = QWidget()
        more = QVBoxLayout(advanced)
        more.setContentsMargins(0, 0, 0, 0)
        number(more, 'step', '步长', ' px', 2)
        number(more, 'duration_ms', '缓动', ' ms', 10)
        box.addWidget(disclosure('高级', advanced), alignment=Qt.AlignLeft)
        box.addWidget(advanced)
        bottom = QHBoxLayout()
        self.status = QLabel('')
        self.status.setWordWrap(True)
        apply = QPushButton('应用')
        apply.clicked.connect(self.save)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(apply)
        box.addLayout(bottom)

    def save(self):
        value = {key: control.isChecked() if key in scroll.FLAGS else control.value()
                 for key, control in self.controls.items()}
        try:
            self.status.setText(self.engine.save_wheel(value))
        except OSError:
            self.status.setText('无法保存滚轮设置')


class InputSettings(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle('键鼠设置')
        self.setFixedWidth(360)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(16)
        self.peer, self.controller, self.side = QComboBox(), QComboBox(), QComboBox()
        self.controller.addItem(window.service.name, window.service.device_id)
        for peer in window.service.peers.values():
            self.peer.addItem(peer['name'], peer['id'])
            self.controller.addItem(peer['name'], peer['id'])
        for name, point in [('右侧', [1, 0]), ('左侧', [-1, 0]), ('上方', [0, -1]), ('下方', [0, 1])]:
            self.side.addItem(name, point)
        for title, widget in [('另一台电脑', self.peer), ('键鼠来自', self.controller), ('位于本机', self.side)]:
            row = QHBoxLayout()
            row.addWidget(QLabel(title))
            row.addStretch()
            row.addWidget(widget)
            box.addLayout(row)
        self.message = QLabel('')
        self.message.setWordWrap(True)
        box.addWidget(self.message)
        buttons = QHBoxLayout()
        self.save = QPushButton('保存布局')
        self.save.clicked.connect(self.save_layout)
        buttons.addWidget(self.save)
        self.start_button = QPushButton('启用键鼠')
        self.start_button.setObjectName('primary')
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button)
        box.addLayout(buttons)
        if sys.platform == 'darwin':
            wheel = WheelSettings(window)
            box.addWidget(disclosure('鼠标滚轮', wheel), alignment=Qt.AlignLeft)
            box.addWidget(wheel)
        self.future = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_saved)
        self.timer.start(100)
        existing = window.engine.layout()
        if existing:
            self.controller.setCurrentIndex(self.controller.findData(existing['controller_id']))
            for i in range(self.peer.count()):
                peer_id = self.peer.itemData(i)
                if peer_id in existing['positions']:
                    self.peer.setCurrentIndex(i)
                    own = existing['positions'][window.service.device_id]
                    other = existing['positions'][peer_id]
                    point = [other[0]-own[0], other[1]-own[1]]
                    self.side.setCurrentIndex(self.side.findData(point))
                    break
        if not window.service.peers:
            self.save.setEnabled(False)
            self.message.setText('请先连接另一台电脑')

    def save_layout(self):
        peer, host = self.peer.currentData(), self.controller.currentData()
        if host not in {peer, self.window.service.device_id}:
            self.message.setText('请选择这两台电脑之一')
            return
        layout = {'version': 1, 'controller_id': host,
                  'positions': {self.window.service.device_id: [0, 0], peer: self.side.currentData()}}
        self.save.setEnabled(False)
        self.message.setText('正在保存')
        self.future = self.window.service.io_pool.submit(self.window.service.configure_input, layout)

    def check_saved(self):
        from PySide6.QtCore import QProcess
        self.start_button.setText('停止键鼠' if self.window.engine.process.state() != QProcess.NotRunning else '启用键鼠')
        if self.future is not None and self.future.done():
            try:
                self.future.result()
                self.message.setText('布局已保存')
            except Exception as error:
                self.message.setText(str(error))
            self.future = None
            self.save.setEnabled(True)

    def start(self):
        try:
            from PySide6.QtCore import QProcess
            if self.window.engine.process.state() != QProcess.NotRunning:
                self.window.engine.stop(disable=True)
                self.message.setText('键鼠已停止')
                return
            self.window.engine.start()
            self.message.setText('正在启用')
        except PermissionError as error:
            import subprocess
            from .engine import mac_drag_helper
            subprocess.Popen([str(mac_drag_helper()), '--request-accessibility'], stdout=subprocess.DEVNULL)
            self.message.setText(str(error))
        except Exception as error:
            self.message.setText(str(error))
