"""Only file clipboard objects. Text, images and input remain with Deskflow."""
import ctypes
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QMimeData, QTimer, QUrl, Signal
from PySide6.QtGui import QClipboard, QGuiApplication

ORIGIN_MIME = "application/x-inputbridge-origin"
X11_PROTOCOL_FORMATS = {"TARGETS", "MULTIPLE", "TIMESTAMP", "SAVE_TARGETS"}


class MacPasteboardCounter:
    """NSPasteboard changes while this menu-bar app is inactive as well."""
    def __init__(self):
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        message = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        object_call = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(message)
        self.counter_call = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(message)
        self.pasteboard = object_call(objc.objc_getClass(b'NSPasteboard'), objc.sel_registerName(b'generalPasteboard'))
        self.selector = objc.sel_registerName(b'changeCount')
        self.objc = objc

    def __call__(self):
        return self.counter_call(self.pasteboard, self.selector)


def file_mime(paths, offer_id):
    mime = QMimeData()
    urls = [QUrl.fromLocalFile(str(Path(p).absolute())) for p in paths]
    mime.setUrls(urls)
    # Nautilus has an explicit copy/cut MIME in addition to text/uri-list.
    mime.setData("x-special/gnome-copied-files", ("copy\n" + "\n".join(u.toString(QUrl.FullyEncoded) for u in urls)).encode())
    mime.setData(ORIGIN_MIME, offer_id.encode())
    return mime


class FileClipboard(QObject):
    changed = Signal(object)

    def __init__(self, clipboard, parent=None, native_counter=None):
        super().__init__(parent)
        self.clipboard = clipboard
        self.awaiting_receive = False
        self.native_counter = native_counter
        if native_counter is None and sys.platform == 'darwin' and QGuiApplication.platformName() == 'cocoa':
            self.native_counter = MacPasteboardCounter()
        self.last_count = self.native_counter() if self.native_counter else None
        clipboard.dataChanged.connect(self.inspect)
        if self.native_counter:
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.poll_native)
            self.timer.start(250)

    def poll_native(self):
        current = self.native_counter()
        if current != self.last_count:
            self.inspect()

    def inspect(self):
        if self.native_counter:
            current = self.native_counter()
            if current == self.last_count:
                return
            self.last_count = current
        mime = self.clipboard.mimeData(QClipboard.Clipboard)
        if mime is not None and mime.hasFormat(ORIGIN_MIME):
            return
        # GNOME's clipboard manager drops a custom-only pending marker. That
        # empty selection is our retirement operation, not a newer user copy.
        formats = set(mime.formats()) if mime is not None else set()
        if self.awaiting_receive and not formats.difference(X11_PROTOCOL_FORMATS):
            return
        self.awaiting_receive = False
        paths = []
        if mime is not None and mime.hasUrls():
            urls = mime.urls()
            # A URL copied from a browser is never interpreted as a file request.
            if urls and all(u.isLocalFile() and u.host() in ("", "localhost") for u in urls):
                paths = [u.toLocalFile() for u in urls]
        self.changed.emit(paths or None)

    def publish(self, paths, offer_id):
        if not all(Path(p).exists() for p in paths):
            raise FileNotFoundError("接收的文件不可用，请重新复制")
        self.clipboard.setMimeData(file_mime(paths, offer_id), QClipboard.Clipboard)
        self.awaiting_receive = False

    def retire_previous_receive(self):
        """Do not leave an older received file pasteable during a new transfer."""
        self.awaiting_receive = True
        current = self.clipboard.mimeData(QClipboard.Clipboard)
        if current is not None and current.hasFormat(ORIGIN_MIME):
            pending = QMimeData()
            pending.setData(ORIGIN_MIME, b"pending")
            self.clipboard.setMimeData(pending, QClipboard.Clipboard)
