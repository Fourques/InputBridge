"""Small native Qt window and tray. No webview, screen streaming or input hooks."""
import os
from pathlib import Path
import platform
import queue
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, QSize, QEvent, QObject, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QNetworkInterface, QAbstractSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)

from .clipboard import FileClipboard
from .engine import InputEngine, find_core

STYLE = """
QWidget { color: #000000; font-size: 13px; }
QMainWindow, QDialog { background: #ffffff; }
QLabel { background: transparent; }
QLabel#title { font-size: 21px; font-weight: 600; letter-spacing: -0.5px; }
QLabel#muted { color: #666666; font-size: 12px; }
QLabel#section { color: #666666; font-size: 12px; }
QLabel#device { font-size: 15px; font-weight: 600; }
QLabel#small { color: #666666; font-size: 11px; }
QLabel#status { color: #000000; font-size: 12px; }
QFrame#card { background: white; border: none; border-bottom: 1px solid #e5e5e5; border-radius: 0; }
QFrame#transfer { background: #f5f5f5; border: none; border-radius: 8px; }
QPushButton { padding: 8px 13px; background: white; border: 1px solid #e5e5e5; border-radius: 7px; }
QPushButton:hover { background: #f5f5f5; border-color: #999999; }
QPushButton:focus { border-color: #000000; }
QPushButton:disabled { color: #999999; }
QPushButton#primary { background: #000000; color: white; border-color: #000000; font-weight: 600; }
QPushButton#primary:hover { background: #333333; }
QPushButton#plain { border: none; background: transparent; color: #000000; padding: 3px 0px; }
QPushButton#plain:hover { color: #666666; }
QPushButton#more { border: none; background: transparent; padding: 3px 9px; font-size: 18px; }
QPushButton#more::menu-indicator { image: none; width: 0; }
QCheckBox { spacing: 10px; }
QCheckBox::indicator { width: 33px; height: 18px; border: 1px solid #e5e5e5; border-radius: 9px; background: #e5e5e5; }
QCheckBox::indicator:checked { background: #000000; border-color: #000000; image: none; }
QLineEdit { background: white; border: 1px solid #e5e5e5; border-radius: 7px; padding: 10px; selection-background-color: #000000; }
QLineEdit:focus { border-color: #000000; }
QProgressBar { border: none; border-radius: 2px; background: #e5e5e5; height: 3px; text-align: center; }
QProgressBar::chunk { border-radius: 2px; background: #000000; }
QScrollArea { border: none; background: transparent; }
"""


def label(text, role=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    if role:
        widget.setObjectName(role)
    return widget


def app_icon(size=64, tray=False):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    if not tray:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#000000"))
        painter.drawRoundedRect(0, 0, size, size, size * .25, size * .25)
    painter.setPen(QPen(QColor("#000000" if tray else "#ffffff"), max(1.5, size * .035)))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(int(size*.17), int(size*.25), int(size*.32), int(size*.36), 2, 2)
    painter.drawRoundedRect(int(size*.53), int(size*.37), int(size*.29), int(size*.32), 2, 2)
    painter.drawLine(int(size*.35), int(size*.74), int(size*.67), int(size*.74))
    painter.end()
    icon = QIcon(pixmap)
    if tray:
        icon.setIsMask(True)
    return icon


def best_lan_address():
    choices = []
    flags = QNetworkInterface.InterfaceFlag
    for interface in QNetworkInterface.allInterfaces():
        if not interface.flags() & flags.IsUp or not interface.flags() & flags.IsRunning or interface.flags() & flags.IsLoopBack:
            continue
        priority = 0 if interface.type() == QNetworkInterface.InterfaceType.Ethernet else 1
        for entry in interface.addressEntries():
            address = entry.ip()
            text = address.toString()
            if address.protocol() == QAbstractSocket.IPv4Protocol and not text.startswith(("127.", "169.254.")):
                choices.append((priority, text))
    return sorted(choices)[0][1] if choices else "127.0.0.1"


class PairDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.setWindowTitle("连接电脑 · InputBridge")
        self.setFixedWidth(380)
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 26, 26, 26)
        box.setSpacing(14)
        box.addWidget(label("连接设备", "device"))
        self.copy = QPushButton("复制本机邀请")
        self.copy.clicked.connect(self.copy_invite)
        box.addWidget(self.copy)
        address = label(controller.service.endpoint.removeprefix("https://"), "small")
        address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(address)
        box.addSpacing(14)
        self.input = QLineEdit()
        self.input.setPlaceholderText("粘贴另一台电脑的邀请")
        self.input.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        box.addWidget(self.input)
        self.connect_button = QPushButton("连接")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self.connect_peer)
        box.addWidget(self.connect_button)
        self.result = label("邀请 10 分钟内有效", "small")
        self.result.setWordWrap(True)
        box.addWidget(self.result)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_pair)
        self.future = None

    def copy_invite(self):
        QApplication.clipboard().setText(self.controller.service.create_invite())
        self.copy.setText("已复制")
        self.result.setText("在另一台电脑粘贴邀请")

    def connect_peer(self):
        self.connect_button.setEnabled(False)
        self.result.setText("连接中…")
        self.future = self.controller.service.io_pool.submit(self.controller.service.pair, self.input.text())
        self.input.clear()
        self.timer.start(150)

    def check_pair(self):
        if self.future and self.future.done():
            self.timer.stop()
            try:
                self.future.result()
                self.accept()
            except Exception as error:
                self.result.setText(str(error))
                self.connect_button.setEnabled(True)


class QuitFilter(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Quit:
            self.window.quitting = True
        return False


class Switch(QCheckBox):
    def __init__(self):
        super().__init__()
        self.setFixedSize(38, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("文件复制粘贴")

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#000000" if self.isChecked() else "#e5e5e5"))
        painter.drawRoundedRect(0, 2, 38, 18, 9, 9)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(22 if self.isChecked() else 3, 5, 12, 12)
        painter.end()


class Window(QMainWindow):
    events_ready = Signal()
    def __init__(self, service, clipboard_enabled=True):
        super().__init__()
        self.quitting = False
        self.quit_filter = QuitFilter(self)
        QApplication.instance().installEventFilter(self.quit_filter)
        self.service = service
        self.engine = InputEngine(service, self)
        self.engine.status_changed.connect(self.set_input_status)
        self.clipboard = FileClipboard(QApplication.clipboard(), self) if clipboard_enabled else None
        if self.clipboard:
            self.clipboard.changed.connect(service.clipboard_changed)
        self.setWindowTitle("InputBridge")
        self.setWindowIcon(app_icon())
        close_action = QAction("关闭窗口", self)
        close_action.setShortcut(QKeySequence.Close)
        close_action.triggered.connect(self.close)
        self.addAction(close_action)
        self.setFixedWidth(400)
        self.resize(400, 360)
        base = QWidget()
        self.setCentralWidget(base)
        layout = QVBoxLayout(base)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)
        top = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(6)
        title_box.addWidget(label("InputBridge", "title"))
        top.addLayout(title_box)
        top.addStretch()
        symbol = QLabel()
        symbol.setPixmap(app_icon().pixmap(30, 30))
        top.addWidget(symbol)
        layout.addLayout(top)
        layout.addSpacing(4)

        card = QFrame()
        card.setObjectName("card")
        card_box = QVBoxLayout(card)
        card_box.setContentsMargins(0, 10, 0, 18)
        card_box.setSpacing(11)
        local_heading = QHBoxLayout()
        local_heading.addWidget(label(service.name, "device"))
        local_heading.addStretch()
        native_os = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(platform.system(), platform.system())
        local_heading.addWidget(label(native_os, "muted"))
        card_box.addLayout(local_heading)
        self.input_status = label("", "small")
        row = QHBoxLayout()
        row.addWidget(label("文件同步"))
        row.addStretch()
        self.enabled = Switch()
        self.enabled.setChecked(not service.paused)
        self.enabled.toggled.connect(lambda enabled: service.set_paused(not enabled))
        self.enabled.setToolTip("开启后，复制的文件会发送给已配对的电脑")
        row.addWidget(self.enabled)
        card_box.addLayout(row)
        layout.addWidget(card)

        heading = QHBoxLayout()
        heading.addWidget(label("设备", "section"))
        heading.addStretch()
        add = QPushButton("＋ 连接")
        add.setObjectName("plain")
        add.clicked.connect(self.add_peer)
        heading.addWidget(add)
        layout.addLayout(heading)

        self.peer_container = QWidget()
        self.peer_layout = QVBoxLayout(self.peer_container)
        self.peer_layout.setContentsMargins(0, 0, 0, 0)
        self.peer_layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.peer_container)
        self.peer_container.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setMinimumHeight(72)
        scroll.setMaximumHeight(180)
        layout.addWidget(scroll, 1)

        transfer = self.transfer = QFrame()
        transfer.setObjectName("transfer")
        transfer_box = QVBoxLayout(transfer)
        transfer_box.setContentsMargins(17, 14, 17, 14)
        transfer_box.setSpacing(7)
        self.transfer_title = label("", "status")
        self.transfer_hint = label("", "muted")
        self.transfer_hint.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        self.cancel = QPushButton("取消")
        self.cancel.setObjectName("plain")
        self.cancel.clicked.connect(self.cancel_transfer)
        self.cancel.hide()
        transfer_box.addWidget(self.transfer_title)
        transfer_box.addWidget(self.transfer_hint)
        transfer_box.addWidget(self.progress)
        transfer_box.addWidget(self.cancel, alignment=Qt.AlignLeft)
        layout.addWidget(transfer)
        transfer.hide()

        bottom = QHBoxLayout()
        settings = QPushButton("键鼠设置")
        settings.setObjectName("plain")
        settings.clicked.connect(self.open_input_settings)
        bottom.addWidget(settings)
        bottom.addStretch()
        bottom.addWidget(self.input_status)
        layout.addLayout(bottom)

        self.tray = QSystemTrayIcon(app_icon(64, tray=True), self)
        self.tray.setToolTip("InputBridge · 文件共享")
        menu = QMenu()
        show = menu.addAction("打开 InputBridge")
        show.triggered.connect(self.show_window)
        self.pause_action = menu.addAction("暂停文件共享")
        self.pause_action.setCheckable(True)
        self.pause_action.setChecked(service.paused)
        self.pause_action.triggered.connect(service.set_paused)
        menu.addSeparator()
        menu.addAction("退出 InputBridge", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.refresh_peers()
        self.engine_timer = QTimer(self)
        self.engine_timer.timeout.connect(self.refresh_engine)
        self.engine_timer.start(15000)
        QTimer.singleShot(0, self.refresh_engine)
        from .drag_bridge import DragBridge
        self.drag = DragBridge(self)
        self.events_ready.connect(self.drain_events, Qt.QueuedConnection)
        self.service.event_wakeup = self.events_ready.emit
        self.events_ready.emit()

    def refresh_engine(self):
        from PySide6.QtCore import QProcess
        if self.engine.process.state() != QProcess.NotRunning:
            self.set_input_status('键鼠引擎运行中')
        elif not self.input_status.text() or not self.engine.path.exists():
            self.set_input_status('键鼠待配置' if find_core() else '输入核心待构建')

    def set_input_status(self, text):
        self.input_status.setText('待切换键鼠' if text.startswith('现有键鼠服务') else text)

    def open_input_settings(self):
        from .input_settings import InputSettings
        InputSettings(self).exec()

    def refresh_peers(self):
        while self.peer_layout.count():
            item = self.peer_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        with self.service.lock:
            peers = [dict(peer) for peer in self.service.peers.values()]
            online = dict(self.service.online)
        if not peers:
            empty = label("尚未连接设备", "muted")
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(65)
            self.peer_layout.addWidget(empty)
        for peer in peers:
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            row.setContentsMargins(0, 10, 0, 10)
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(label(peer["name"]))
            column.addWidget(label("已连接" if online.get(peer["id"]) else "未连接", "status" if online.get(peer["id"]) else "muted"))
            row.addLayout(column)
            row.addStretch()
            more = QPushButton("⋯")
            more.setObjectName("more")
            peer_menu = QMenu(more)
            action = peer_menu.addAction("移除这台电脑")
            action.triggered.connect(lambda checked=False, ident=peer["id"]: self.service.remove_peer(ident))
            more.setMenu(peer_menu)
            row.addWidget(more)
            self.peer_layout.addWidget(card)
        self.peer_layout.addStretch()

    def add_peer(self):
        PairDialog(self).exec()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def cancel_transfer(self):
        self.service.receive_cancel.set()
        self.service.build_cancel.set()

    def drain_events(self):
        while True:
            try:
                event = self.service.events.get_nowait()
            except queue.Empty:
                break
            self.apply_event(event)

    def apply_event(self, event):
        kind = event["kind"]
        if "generation" in event and event["generation"] != self.service.receive_generation:
            return
        if kind == "peers":
            self.engine.revoke_removed_peers()
            self.refresh_peers()
        elif kind == 'input-layout':
            self.engine.save(event['layout'])
        elif kind.startswith('drag-'):
            try:
                self.drag.service_event(event)
            except Exception as error:
                self.drag.fail(error)
        elif kind == "paused":
            if event['paused']:
                self.drag.stop()
            else:
                from PySide6.QtCore import QProcess
                if self.engine.process.state() == QProcess.Running:
                    self.drag.start()
            self.enabled.blockSignals(True)
            self.enabled.setChecked(not event["paused"])
            self.enabled.blockSignals(False)
            self.pause_action.setChecked(event["paused"])
            self.transfer_title.setText("已暂停" if event["paused"] else "已恢复")
            self.transfer_hint.setText("")
            self.transfer.setVisible(event["paused"])
            self.progress.hide()
            self.cancel.hide()
        elif kind == "preparing":
            self.transfer.show()
            self.transfer_title.setText("准备文件…")
            self.transfer_hint.setText("")
            self.cancel.show()
        elif kind == "offered":
            self.transfer.show()
            self.transfer_title.setText("已准备 %s 项" % event["count"])
            self.transfer_hint.setText(human_size(event["total"]))
            self.cancel.hide()
        elif kind == "receiving":
            self.transfer.show()
            if self.clipboard:
                self.clipboard.retire_previous_receive()
            self.transfer_title.setText("正在接收")
            self.transfer_hint.setText(event["peer"])
            self.progress.setValue(0)
            self.progress.show()
            self.cancel.show()
        elif kind == "progress":
            self.progress.setValue(int(100 * event["done"] / max(event["total"], 1)))
        elif kind == "ready":
            with self.service.lock:
                valid = (event["revision"] == self.service.revision and not self.service.paused and
                         event["generation"] == self.service.receive_generation)
            if not valid:
                return
            if self.clipboard:
                try:
                    self.clipboard.publish(event["paths"], event["offer_id"], event.get("image", False))
                    self.service.clipboard_changed(None)
                except OSError as error:
                    self.apply_event({"kind": "error", "message": str(error)})
                    return
            self.transfer.show()
            self.transfer_title.setText("已接收图片" if event.get("image") else "已接收 %s 项" % len(event["paths"]))
            self.transfer_hint.setText("可以粘贴")
            self.progress.hide()
            self.cancel.hide()
        elif kind in ("error", "cancelled"):
            self.transfer.show()
            self.transfer_title.setText("传输失败" if kind == "error" else "已取消")
            self.transfer_hint.setText(event.get("message", ""))
            self.progress.hide()
            self.cancel.hide()

    def closeEvent(self, event):
        if self.quitting:
            event.accept()
            return
        if self.tray.isVisible():
            self.hide()
        else:
            self.showMinimized()
        event.ignore()

    def quit_app(self):
        self.quitting = True
        self.service.event_wakeup = None
        self.drag.stop()
        self.engine.stop()
        self.tray.hide()
        QApplication.quit()


def human_size(value):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return ("%.1f %s" if unit != "B" else "%.0f %s") % (value, unit)
        value /= 1024
