"""Native drag endpoints: AppKit promises and X11 URI drags, with real OS targets."""
import base64
import ctypes
import json
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QByteArray, QEventLoop, QMetaType, QMimeData, QMimeDatabase, QObject, QPoint, QProcess, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QGuiApplication, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import QWidget

from .files import TransferError
from .images import MAX_PREVIEW_BYTES, decode_raster

DRAG_MIME = 'application/x-inputbridge-drag'
XDS_MIME = 'XdndDirectSave0'


def draw_file_icon(painter, category):
    painter.save()
    painter.translate(51, 16)
    white = QColor('#f2f2f2')
    painter.setPen(QPen(white, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    if category == 'folder':
        painter.drawPolygon(QPolygon([QPoint(2, 11), QPoint(2, 5), QPoint(14, 5), QPoint(19, 11),
                                      QPoint(39, 11), QPoint(39, 36), QPoint(2, 36)]))
    elif category in ('image', 'video'):
        painter.drawRoundedRect(QRect(1, 5, 40, 31), 5, 5)
        if category == 'image':
            painter.drawEllipse(QRect(25, 11, 6, 6))
            painter.drawPolyline(QPolygon([QPoint(3, 31), QPoint(15, 19), QPoint(23, 27),
                                          QPoint(29, 22), QPoint(39, 32)]))
        else:
            painter.setBrush(white)
            painter.drawPolygon(QPolygon([QPoint(16, 12), QPoint(29, 20), QPoint(16, 29)]))
    elif category == 'audio':
        for x, height in zip((5, 13, 21, 29, 37), (9, 20, 31, 24, 12)):
            painter.drawLine(x, 20 - height // 2, x, 20 + height // 2)
    elif category == 'archive':
        painter.drawRoundedRect(QRect(4, 4, 34, 33), 4, 4)
        painter.drawLine(4, 12, 38, 12)
        painter.drawRoundedRect(QRect(18, 17, 6, 11), 2, 2)
    else:
        if category == 'multiple':
            painter.drawPolyline(QPolygon([QPoint(10, 1), QPoint(37, 1), QPoint(37, 32)]))
        painter.drawPolygon(QPolygon([QPoint(6, 5), QPoint(22, 5), QPoint(33, 16),
                                      QPoint(33, 39), QPoint(6, 39)]))
        painter.drawPolyline(QPolygon([QPoint(22, 5), QPoint(22, 16), QPoint(33, 16)]))
        painter.drawLine(13, 23, 26, 23)
        painter.drawLine(13, 30, 23, 30)
    painter.restore()


def drag_pixmap(manifest):
    roots = set(manifest["roots"])
    entries = [entry for entry in manifest["entries"] if entry["path"] in roots]
    label = f'{len(entries)} 项'
    badge = ''
    category = 'multiple'
    thumbnail = None
    if len(entries) == 1 and entries[0]["kind"] == "file":
        label = entries[0]["path"]
        badge = Path(label).suffix.removeprefix('.').upper()[:7]
        encoded = manifest.get("preview_png")
        if isinstance(encoded, str) and len(encoded) <= 4 * ((MAX_PREVIEW_BYTES + 2) // 3):
            try:
                image = decode_raster(base64.b64decode(encoded, validate=True), b"png", preview=True)
                if not image.isNull():
                    thumbnail = QPixmap.fromImage(image)
            except ValueError:
                pass
        media = QMimeDatabase().mimeTypeForFile(label, QMimeDatabase.MatchExtension)
        category = media.name().partition('/')[0]
        if media.genericIconName() == 'package-x-generic' or badge in ('ZIP', '7Z', 'RAR', 'TAR', 'GZ'):
            category = 'archive'
    elif len(entries) == 1:
        label = entries[0]["path"]
        category = 'folder'

    pixmap = QPixmap(144, 98)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(29, 29, 29, 232))
        painter.drawRoundedRect(QRect(1, 1, 142, 96), 12, 12)
        if thumbnail is None:
            draw_file_icon(painter, category)
        else:
            position = QPoint((144 - thumbnail.width()) // 2, 6 + (64 - thumbnail.height()) // 2)
            painter.drawPixmap(position, thumbnail)
        if badge:
            badge_width = painter.fontMetrics().horizontalAdvance(badge) + 12
            badge_rect = QRect(128 - badge_width, 54, badge_width, 18)
            painter.setBrush(QColor('#f0f0f3'))
            painter.drawRoundedRect(badge_rect, 4, 4)
            painter.setPen(QColor('#242429'))
            painter.drawText(badge_rect, Qt.AlignCenter, badge)
        painter.setPen(QColor('#ffffff'))
        caption = painter.fontMetrics().elidedText(label, Qt.ElideMiddle, 126)
        painter.drawText(QRect(9, 74, 126, 18), Qt.AlignCenter, caption)
    finally:
        painter.end()
    return pixmap


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

    def resume_local(self, drag_id, paths, manifest):
        self.send({'op': 'resumeLocal', 'drag': drag_id, 'paths': paths,
                   'preview': manifest.get('preview_png')})

    def released(self, drag_id):
        self.send({'op': 'buttonUp', 'drag': drag_id})

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
        self.x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.x.XInternAtom.restype = ctypes.c_ulong
        self.x.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x.XGetSelectionOwner.restype = ctypes.c_ulong
        self.x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self.x.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
            ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
        self.x.XFree.argtypes = [ctypes.c_void_p]
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

    def offer_direct_save(self, name):
        selection = self.x.XInternAtom(self.display, b'XdndSelection', 0)
        window = self.x.XGetSelectionOwner(self.display, selection)
        if not window:
            return None
        prop = self.x.XInternAtom(self.display, XDS_MIME.encode(), 0)
        text = self.x.XInternAtom(self.display, b'text/plain', 0)
        raw = name.encode('utf-8')
        self.x.XChangeProperty(self.display, window, prop, text, 8, 0, raw, len(raw))
        self.x.XSync(self.display, 0)
        return (window, prop)

    def direct_save_destination(self, window, prop):
        if self.held():
            return None
        kind, count, rest = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
        width, result = ctypes.c_int(), ctypes.c_void_p()
        status = self.x.XGetWindowProperty(self.display, window, prop, 0, 4096, 0, 0,
            ctypes.byref(kind), ctypes.byref(width), ctypes.byref(count), ctypes.byref(rest), ctypes.byref(result))
        try:
            if status or not result or width.value != 8 or rest.value or not count.value:
                return None
            uri = QUrl.fromEncoded(ctypes.string_at(result, count.value), QUrl.StrictMode)
            if uri.isLocalFile() and uri.host() in ('', 'localhost') and not uri.hasQuery() and not uri.hasFragment():
                path = uri.toLocalFile()
                if '\x00' not in path and Path(path).is_absolute():
                    return path
        finally:
            if result:
                self.x.XFree(result)

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


def wait_for_result(future):
    if not future.done():
        loop = QEventLoop()
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if future.done() else None)
        timer.start(20)
        QTimer.singleShot(120000, loop.quit)
        loop.exec()
        timer.stop()
    if not future.done():
        raise TransferError('等待接收超时')
    return future.result()


class LazyFiles(QMimeData):
    def __init__(self, future, drag_id, direct_save=None):
        super().__init__()
        self.future, self.drag_id = future, drag_id
        self.direct_save = direct_save

    def formats(self):
        direct = [XDS_MIME] if self.direct_save else []
        return direct + ['text/uri-list', DRAG_MIME]

    def retrieveData(self, mime, preferred):
        if mime == DRAG_MIME:
            return QByteArray(self.drag_id.encode())
        if mime == XDS_MIME and self.direct_save:
            return QByteArray(self.direct_save())
        if mime != 'text/uri-list':
            return QByteArray()
        try:
            paths = wait_for_result(self.future)
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
        self.local_paths = None
        self.local_handoff = None
        self.pending_resume = None
        self.direct_drag = None
        self.cleared_edge = False
        self.source_point = None
        self.source_drag = None
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
            if self.source_drag or not self.pointer.held():
                return
            self.source_drag = event['drag']
            self.source_point = self.pointer.point
            if self.local_paths is not None:
                self.local_handoff = event['drag']
            self.event.emit(event)
        strip.source.connect(source)
        self.strips.append(strip)
        self.timer.start(16)

    def tick(self):
        held = self.pointer.held()
        if not held:
            self.source_drag = self.source_point = None
        for strip in self.strips:
            if self.local_paths is not None and not self.cleared_edge:
                edge = strip.geometry()
                x, y = self.pointer.point
                self.cleared_edge = abs(x - edge.center().x()) >= 32 if edge.width() == 3 else abs(y - edge.center().y()) >= 32
            strip.setVisible(held and (self.current is None or self.cleared_edge))

    def swallow(self, drag_id):
        if self.local_handoff == drag_id and self.current:
            self.cancel(self.current)
        elif self.pointer and self.source_drag == drag_id and self.pointer.held():
            self.pointer.release_source_drag(self.source_point)

    def resume_local(self, drag_id, paths, manifest):
        if self.current and self.local_paths is not None:
            if self.pending_resume:
                self.event.emit({'event': 'dragEnded', 'drag': drag_id, 'result': 'failed'})
                return
            self.pending_resume = (drag_id, paths, manifest)
            self.cancel(self.current)
            return
        self.incoming(drag_id, manifest, None, paths)

    def resume_after_cancel(self):
        pending, self.pending_resume = self.pending_resume, None
        if pending and self.pointer:
            self.resume_local(*pending)

    def incoming(self, drag_id, manifest, future, local_paths=None, save=None):
        if self.current:
            self.event.emit({'event': 'dragEnded', 'drag': drag_id, 'result': 'none'})
            return
        self.current = drag_id
        self.local_paths = local_paths
        self.cleared_edge = False
        self.future = future
        self.manifest = manifest
        self.save = save
        self.started_at = time.monotonic()
        point = QCursor.pos()
        self.anchor.setGeometry(point.x()-80, point.y()-80, 160, 160)
        self.anchor.show()
        self.anchor.raise_()
        QTimer.singleShot(0, lambda: self.arm_target(drag_id))

    def arm_target(self, drag_id):
        if self.current != drag_id:
            return
        if self.current in self.release_ids:
            self.finish('none')
            return
        window = self.anchor.windowHandle()
        if window is None or not window.isExposed():
            if time.monotonic()-self.started_at < .5:
                QTimer.singleShot(8, lambda: self.arm_target(drag_id))
            else:
                self.finish('failed')
            return
        # No button is replayed by the core on entry: pressing into the mapped
        # anchor makes the XDND initiator and the implicit X11 grab the same client.
        if self.pointer.held():
            self.finish('failed')
            return
        # The cursor may already have moved away from the point where the
        # anchor was mapped. Keep the transparent source beneath it.
        point = QCursor.pos()
        self.anchor.move(point.x() - self.anchor.width() // 2, point.y() - self.anchor.height() // 2)
        self.anchor.raise_()
        QGuiApplication.sync()
        if self.current != drag_id or self.dragging or drag_id in self.release_ids:
            return
        self.injected_down = True
        self.pointer.press_target()
        QTimer.singleShot(500, lambda: self.finish('failed')
            if self.current == drag_id and not self.dragging else None)

    def execute_drag(self):
        if not self.current or self.dragging:
            return
        if self.current in self.release_ids:
            self.finish('none')
            return
        drag_id = self.current
        self.dragging = True
        drag = QDrag(self.anchor)
        self.direct_drag = None
        if self.local_paths is None:
            direct = self.offer_save(drag_id) if self.save else None
            drag.setMimeData(LazyFiles(self.future, drag_id, direct))
        else:
            urls = QMimeData()
            urls.setUrls([QUrl.fromLocalFile(path) for path in self.local_paths])
            drag.setMimeData(urls)
        pixmap = drag_pixmap(self.manifest)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(12, 12))
        try:
            self.event.emit({'event': 'dragStarted', 'drag': drag_id})
            actions = Qt.CopyAction if self.local_paths is None else Qt.CopyAction | Qt.MoveAction
            result = drag.exec(actions)
            self.event.emit({'event': 'dragEnded', 'drag': drag_id,
                             'result': 'dropped' if result in (Qt.CopyAction, Qt.MoveAction) else 'none'})
        finally:
            self.dragging = False
            self.clear()

    def offer_save(self, drag_id):
        name = self.manifest['roots'][0]
        state = {'window': None, 'status': None}
        callback = self.save
        self.direct_drag = drag_id

        def advertise():
            if self.pointer and self.dragging and self.current == drag_id:
                state['window'] = self.pointer.offer_direct_save(name)

        def save():
            if self.direct_drag != drag_id or not self.pointer or not state['window']:
                return b'E'
            if state['status'] is not None:
                return state['status']
            path = self.pointer.direct_save_destination(*state['window'])
            if not path or Path(path).name != name:
                return b'E'
            state['status'] = b'E'
            try:
                wait_for_result(callback(path))
                state['status'] = b'S'
            except (OSError, TransferError):
                pass
            return state['status']

        QTimer.singleShot(0, advertise)
        return save

    def released(self, drag_id):
        self.release_ids.add(drag_id)
        if len(self.release_ids) > 16:
            self.release_ids = {drag_id}
        if self.current == drag_id:
            if not self.dragging:
                self.finish('none')
            elif self.local_paths is not None and self.pointer.held():
                self.pointer.release_source_drag()

    def clear(self):
        self.current = None
        self.local_paths = None
        self.local_handoff = None
        self.cleared_edge = False
        self.anchor.hide()
        if self.injected_down and self.pointer and self.pointer.held():
            self.pointer.release_source_drag()
        self.injected_down = False
        if self.pending_resume:
            QTimer.singleShot(0, self.resume_after_cancel)

    def finish(self, result):
        if not self.current or self.dragging:
            return
        ident = self.current
        self.clear()
        self.event.emit({'event': 'dragEnded', 'drag': ident, 'result': result})

    def cancel(self, drag_id=None):
        if self.pending_resume and drag_id in (None, self.pending_resume[0]):
            self.pending_resume = None
        if drag_id is not None and self.current != drag_id:
            return
        if self.dragging:
            QDrag.cancel()
        else:
            self.finish('none')

    def stop(self):
        self.direct_drag = None
        self.cancel()
        self.timer.stop()
        for strip in self.strips:
            strip.hide()
            strip.deleteLater()
        self.strips.clear()
        self.source_drag = self.source_point = None
        self.anchor.hide()
        if self.pointer:
            self.pointer.close()
            self.pointer = None
