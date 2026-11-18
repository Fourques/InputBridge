import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

from inputbridge.clipboard import FileClipboard, ORIGIN_MIME


class ClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clipboard = self.app.clipboard()
        self.adapter = FileClipboard(self.clipboard)
        self.events = []
        self.adapter.changed.connect(self.events.append)

    def tearDown(self):
        self.adapter.changed.disconnect()
        self.clipboard.dataChanged.disconnect(self.adapter.inspect)
        self.clipboard.clear()

    def test_native_file_urls_and_linux_copy_mime_without_echo(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "测试 空格.txt"
            path.write_text("hello")
            self.adapter.publish([path], "offer1")
            mime = self.clipboard.mimeData()
            self.assertEqual(mime.urls()[0].toLocalFile(), str(path))
            self.assertTrue(mime.hasFormat(ORIGIN_MIME))
            self.assertTrue(bytes(mime.data("x-special/gnome-copied-files")).startswith(b"copy\nfile:"))
            self.assertFalse(self.events)

    def test_text_and_web_urls_are_not_file_requests(self):
        self.clipboard.setText("/home/user/not-actually-a-copied-file")
        self.assertIsNone(self.events[-1])
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/report.pdf")])
        self.clipboard.setMimeData(mime)
        self.assertIsNone(self.events[-1])

    def test_user_file_copy_emits_paths(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/example.txt")])
        self.clipboard.setMimeData(mime)
        self.assertEqual(self.events[-1], ["/tmp/example.txt"])

    def test_missing_receive_is_not_published(self):
        with self.assertRaises(FileNotFoundError):
            self.adapter.publish(["/a/path/that/does/not/exist"], "offer")

    def test_new_receive_retires_previous_remote_files_without_echo(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "old.txt"
            path.write_text("old")
            self.adapter.publish([path], "old-offer")
            self.adapter.retire_previous_receive()
            self.assertFalse(self.clipboard.mimeData().hasUrls())
            self.assertFalse(self.events)

    def test_pending_receive_does_not_erase_local_text(self):
        self.clipboard.setText("a newer local copy")
        self.adapter.retire_previous_receive()
        self.assertEqual(self.clipboard.text(), "a newer local copy")

    def test_gnome_dropping_pending_marker_does_not_cancel_next_receive(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'old.txt'
            path.write_text('old')
            self.adapter.publish([path], 'old')
            self.adapter.retire_previous_receive()
            empty = QMimeData()
            for name in ['TARGETS', 'MULTIPLE', 'TIMESTAMP']:
                empty.setData(name, b'')
            self.clipboard.setMimeData(empty)
            self.assertFalse(self.events)
            self.clipboard.setText('a real newer local copy')
            self.assertEqual(self.events, [None])
            self.assertEqual(self.clipboard.text(), 'a real newer local copy')
            self.events.clear()
            # First receive after restart has no previous origin marker either.
            self.adapter.retire_previous_receive()
            self.clipboard.setMimeData(QMimeData())
            self.assertFalse(self.events)
            self.clipboard.setText('another newer copy')
            self.assertEqual(self.events, [None])

    def test_inactive_mac_counter_detects_copy_without_qt_signal(self):
        counter = [10]
        adapter = FileClipboard(self.clipboard, native_counter=lambda: counter[0])
        found = []
        adapter.changed.connect(found.append)
        self.clipboard.blockSignals(True)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile('/tmp/background.txt')])
        self.clipboard.setMimeData(mime)
        self.clipboard.blockSignals(False)
        counter[0] += 1
        adapter.poll_native()
        self.assertEqual(found, [['/tmp/background.txt']])
        adapter.poll_native()
        self.assertEqual(len(found), 1)
        counter[0] += 1  # Same file copied again is still a new user action.
        adapter.poll_native()
        self.assertEqual(len(found), 2)
        self.clipboard.dataChanged.disconnect(adapter.inspect)
        adapter.timer.stop()


if __name__ == "__main__":
    unittest.main()
