"""Bounded raster decoding for the clipboard and drag previews."""
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader

from .files import MAX_IMAGE_BYTES

IMAGE_FORMATS = {
    "png": b"png", "jpg": b"jpeg", "jpeg": b"jpeg", "webp": b"webp",
    "tiff": b"tiff", "tif": b"tiff", "gif": b"gif", "bmp": b"bmp",
}
MAX_IMAGE_PIXELS = 24_000_000
PREVIEW_EDGE = 64
MAX_PREVIEW_BYTES = 20 * 1024


def bounded(size, max_pixels=MAX_IMAGE_PIXELS):
    return size.width() > 0 and size.height() > 0 and size.width() * size.height() <= max_pixels


def decode_raster(source, image_format=None, preview=False):
    if isinstance(source, bytes):
        if len(source) > (MAX_PREVIEW_BYTES if preview else MAX_IMAGE_BYTES):
            return QImage()
        buffer = QBuffer()
        buffer.setData(source)
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer, image_format)
    else:
        path = Path(source)
        image_format = IMAGE_FORMATS.get(path.suffix.lstrip(".").lower())
        if not image_format or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            return QImage()
        reader = QImageReader(str(path), image_format)
    reader.setAutoDetectImageFormat(False)
    size = reader.size()
    if not bounded(size):
        return QImage()
    if preview:
        if isinstance(source, bytes):
            if size.width() > PREVIEW_EDGE or size.height() > PREVIEW_EDGE:
                return QImage()
        elif size.width() > PREVIEW_EDGE or size.height() > PREVIEW_EDGE:
            size.scale(QSize(PREVIEW_EDGE, PREVIEW_EDGE), Qt.KeepAspectRatio)
            reader.setScaledSize(size)
    reader.setAutoTransform(True)
    image = reader.read()
    if preview and (image.width() > PREVIEW_EDGE or image.height() > PREVIEW_EDGE):
        return QImage()
    return image


def png_bytes(image):
    if not bounded(image.size()):
        return b""
    output = QBuffer()
    output.open(QIODevice.WriteOnly)
    if image.save(output, "PNG") and output.size() <= MAX_IMAGE_BYTES:
        return bytes(output.data())
    return b""
