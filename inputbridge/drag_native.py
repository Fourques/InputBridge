"""Native drag endpoints: AppKit promises and X11 URI drags, with real OS targets."""
import ctypes
import json
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QByteArray, QEventLoop, QMetaType, QMimeData, QObject, QPoint, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDrag, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QFileIconProvider, QWidget

from .files import TransferError

DRAG_MIME = 'application/x-inputbridge-drag'


class MacDrag(QObject):
    event = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.pending = b''

    def start(self, side):
        root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1] / 'local'
        binary = root / 'engine/inputbridge-drag-macos'
        if not binary.is_file():
            raise TransferError('原生拖放组件尚未构建')
        self.process.setProgram(str(binary))
        self.process.start()
        if not self.process.waitForStarted(1000):
            raise TransferError('原生拖放组件启动失败')
        self.send({'op': 'edges', 'side': side})

    def send(self, value):
        if self.process.state() == QProcess.Running:
            self.process.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')

    def read(self):
        self.pending += bytes(self.process.readAllStandardOutput())
        if len(self.pending) > 65536:
            self.pending = b''
            return
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                self.event.emit(value)

    def stop(self):
        self.send({'op': 'stop'})
        if not self.process.waitForFinished(1000):
            self.process.kill()
            self.process.waitForFinished(500)


class X11Pointer:
    def __init__(self):
        self.x = ctypes.CDLL('libX11.so.6')
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise TransferError('X11 显示不可用')
        self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong
        self.root = self.x.XDefaultRootWindow(self.display)
        self.x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]

    def held(self):
        rr, child = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = (ctypes.c_int() for _ in range(4))
        mask = ctypes.c_uint()
        self.x.XQueryPointer(self.display, self.root, ctypes.byref(rr), ctypes.byref(child),
            ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        self.point = (rx.value, ry.value)
        return bool(mask.value & 256)

    def release_source_drag(self, point=None):
        xtst = ctypes.CDLL('libXtst.so.6')
        if point:
            xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
            xtst.XTestFakeMotionEvent(self.display, -1, point[0], point[1], 0)
        xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        xtst.XTestFakeButtonEvent(self.display, 1, 0, 0)
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XFlush(self.display)

    def press_target(self):
        xtst = ctypes.CDLL('libXtst.so.6')
        xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        xtst.XTestFakeButtonEvent(self.display, 1, 1, 0)
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XFlush(self.display)

    def close(self):
        if self.display:
            self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
            self.x.XCloseDisplay(self.display)
            self.display = None


class EdgeStrip(QWidget):
    source = Signal(object)

    def __init__(self, rect):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setGeometry(rect)
        self.setWindowOpacity(.01)
        self.setAcceptDrops(True)
        self.drag_id = None

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(DRAG_MIME) or not mime.hasUrls():
            event.ignore()
            return
        urls = mime.urls()
        if not urls or len(urls) > 64 or not all(u.isLocalFile() and u.host() in ('', 'localhost') for u in urls):
            event.ignore()
            return
        if self.drag_id is None:
            self.drag_id = uuid.uuid4().hex
            self.source.emit({'event': 'edgeDrag', 'drag': self.drag_id, 'paths': [u.toLocalFile() for u in urls]})
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def dragMoveEvent(self, event):
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event):
        self.drag_id = None

    def dropEvent(self, event):
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.drag_id = None


class LazyFiles(QMimeData):
    def __init__(self, future, drag_id):
        super().__init__()
        self.future, self.drag_id = future, drag_id

    def formats(self):
        return ['text/uri-list', DRAG_MIME]

    def retrieveData(self, mime, preferred):
        if mime == DRAG_MIME:
            return QByteArray(self.drag_id.encode())
        if mime != 'text/uri-list':
            return QByteArray()
        # URI conversion is requested by the actual drop target; no active-folder guess.
        if not self.future.done():
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if self.future.done() else None)
            timer.start(20)
            QTimer.singleShot(120000, loop.quit)
            loop.exec()
            timer.stop()
        if not self.future.done():
            return QByteArray()
        try:
            paths = self.future.result()
            urls = [QUrl.fromLocalFile(str(Path(p).resolve())) for p in paths]
            if preferred.id() == QMetaType.QVariantList:
                return urls
            raw = b'\r\n'.join(bytes(u.toEncoded()) for u in urls) + b'\r\n'
            return raw.decode() if preferred.id() == QMetaType.QString else QByteArray(raw)
        except Exception:
            return QByteArray()


class DragAnchor(QWidget):
    """Start XDND from our own native mouse press, so our X client owns the grab."""
    def __init__(self, owner):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.owner = owner
        self.setWindowOpacity(.01)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.owner.execute_drag()
            event.accept()


class LinuxDrag(QObject):
    event = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pointer = None
        self.strips = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.anchor = DragAnchor(self)
        self.current = None
        self.source_point = None
        self.dragging = False
        self.injected_down = False
        self.release_ids = set()

    def start(self, side):
        if QGuiApplication.platformName() != 'xcb':
            raise TransferError('此版原生拖放需要 X11')
        self.pointer = X11Pointer()
        screens = QGuiApplication.screens()
        outer = screens[0].geometry()
        for screen in screens[1:]:
            outer = outer.united(screen.geometry())
        from PySide6.QtCore import QRect
        if side == 'left': rect = QRect(outer.left(), outer.top(), 3, outer.height())
        elif side == 'right': rect = QRect(outer.right()-2, outer.top(), 3, outer.height())
        elif side == 'up': rect = QRect(outer.left(), outer.top(), outer.width(), 3)
        else: rect = QRect(outer.left(), outer.bottom()-2, outer.width(), 3)
        strip = EdgeStrip(rect)
        def source(event):
            self.pointer.held()
            self.source_point = self.pointer.point
            self.event.emit(event)
        strip.source.connect(source)
        self.strips.append(strip)
        self.timer.start(16)

    def tick(self):
        held = self.pointer.held()
        for strip in self.strips:
            strip.setVisible(held and self.current is None)

    def swallow(self):
        self.pointer.release_source_drag(self.source_point)

    def incoming(self, drag_id, manifest, future):
        if self.current:
            self.event.emit({'event': 'dragEnded', 'drag': drag_id, 'result': 'none'})
            return
        self.current = drag_id
        self.future = future
        self.started_at = time.monotonic()
        point = QCursor.pos()
        self.anchor.setGeometry(point.x()-80, point.y()-80, 160, 160)
        self.anchor.show()
        self.anchor.raise_()
        QTimer.singleShot(30, self.arm_target)

    def arm_target(self):
        if not self.current:
            return
        if self.current in self.release_ids:
            self.finish('none')
            return
        window = self.anchor.windowHandle()
        if window is None or not window.isExposed():
            if time.monotonic()-self.started_at < .5:
                QTimer.singleShot(20, self.arm_target)
            else:
                self.finish('failed')
            return
        # No button is replayed by the core on entry: pressing into the mapped
        # anchor makes the XDND initiator and the implicit X11 grab the same client.
        if self.pointer.held():
            self.finish('failed')
            return
        self.injected_down = True
        self.pointer.press_target()
        QTimer.singleShot(500, lambda: self.finish('failed')
            if self.current and not self.dragging else None)

    def execute_drag(self):
        if not self.current or self.dragging:
            return
        if self.current in self.release_ids:
            self.finish('none')
            return
        drag_id = self.current
        self.dragging = True
        drag = QDrag(self.anchor)
        drag.setMimeData(LazyFiles(self.future, drag_id))
        icon = QIcon.fromTheme('folder', QFileIconProvider().icon(QFileIconProvider.Folder))
        drag.setPixmap(icon.pixmap(48, 48))
        drag.setHotSpot(QPoint(20, 20))
        try:
            self.event.emit({'event': 'dragStarted', 'drag': drag_id})
            result = drag.exec(Qt.CopyAction, Qt.CopyAction)
            self.event.emit({'event': 'dragEnded', 'drag': drag_id,
                             'result': 'dropped' if result == Qt.CopyAction else 'none'})
        finally:
            self.dragging = False
            self.clear()

    def released(self, drag_id):
        self.release_ids.add(drag_id)
        if len(self.release_ids) > 16:
            self.release_ids = {drag_id}
        if self.current == drag_id and not self.dragging:
            self.finish('none')

    def clear(self):
        self.current = None
        self.anchor.hide()
        if self.injected_down and self.pointer and self.pointer.held():
            self.pointer.release_source_drag()
        self.injected_down = False

    def finish(self, result):
        if not self.current or self.dragging:
            return
        ident = self.current
        self.clear()
        self.event.emit({'event': 'dragEnded', 'drag': ident, 'result': result})

    def cancel(self):
        if self.dragging:
            QDrag.cancel()
        else:
            self.finish('none')

    def stop(self):
        self.cancel()
        self.timer.stop()
        for strip in self.strips:
            strip.hide()
            strip.deleteLater()
        self.strips.clear()
        self.anchor.hide()
        if self.pointer:
            self.pointer.close()
            self.pointer = None
