"""Offscreen Qt contracts only: no real clipboard, pointer or window system."""
from concurrent.futures import Future
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from inputbridge.drag_native import LazyFiles, LinuxDrag


class NativeDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_file_urls_work_for_both_qt_url_consumers_and_external_uri_targets(self):
        future = Future()
        path = Path('/tmp/中文 文件夹').resolve()
        future.set_result([path])
        mime = LazyFiles(future, 'a'*32)
        self.assertEqual([Path(u.toLocalFile()) for u in mime.urls()], [path])
        uri = bytes(mime.data('text/uri-list'))
        self.assertTrue(uri.startswith(b'file:///'))
        self.assertIn(b'%20', uri)
        self.assertTrue(uri.endswith(b'\r\n'))

    def test_fast_release_cancels_before_injecting_a_new_target_press(self):
        endpoint = LinuxDrag()
        pointer = MagicMock()
        endpoint.pointer = pointer
        events = []
        endpoint.event.connect(events.append)
        future = Future()
        future.set_result([])
        try:
            endpoint.released('b'*32)
            endpoint.incoming('b'*32, {}, future)
            endpoint.arm_target()
            pointer.press_target.assert_not_called()
            pointer.release_source_drag.assert_not_called()
            self.assertIsNone(endpoint.current)
            self.assertEqual(events[-1]['result'], 'none')
        finally:
            endpoint.stop()
