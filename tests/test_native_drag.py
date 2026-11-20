"""Offscreen Qt contracts only: no real clipboard, pointer or window system."""
from concurrent.futures import Future
import base64
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
from inputbridge.drag_bridge import DragBridge
from inputbridge.drag_native import LazyFiles, LinuxDrag, drag_pixmap
from inputbridge.engine import screen_name
from inputbridge.images import png_bytes


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
            endpoint.arm_target('b'*32)
            pointer.press_target.assert_not_called()
            pointer.release_source_drag.assert_not_called()
            self.assertIsNone(endpoint.current)
            self.assertEqual(events[-1]['result'], 'none')
        finally:
            endpoint.stop()

    def test_image_drag_uses_thumbnail_before_file_is_available(self):
        thumbnail = QImage(64, 30, QImage.Format_RGB32)
        thumbnail.fill(0xFF33AA66)
        manifest = {"roots": ["照片.jpg"], "entries": [{"path": "照片.jpg", "kind": "file"}],
                    "preview_png": base64.b64encode(png_bytes(thumbnail)).decode("ascii")}
        preview = drag_pixmap(manifest).toImage()
        self.assertEqual(preview.pixelColor(72, 32), thumbnail.pixelColor(10, 10))

    def test_stale_target_callback_and_swallow_do_not_touch_new_drag(self):
        endpoint = LinuxDrag()
        endpoint.pointer = MagicMock()
        endpoint.current = 'b' * 32
        endpoint.source_drag = 'b' * 32
        endpoint.arm_target('a' * 32)
        endpoint.swallow('a' * 32)
        endpoint.cancel('a' * 32)
        self.assertEqual(endpoint.current, 'b' * 32)
        endpoint.pointer.press_target.assert_not_called()
        endpoint.pointer.release_source_drag.assert_not_called()

    def test_resumed_source_is_released_after_native_pointer_leaves(self):
        endpoint = LinuxDrag()
        endpoint.pointer = MagicMock()
        endpoint.pointer.held.return_value = False
        endpoint.current = 'a' * 32
        endpoint.local_paths = ['/tmp/image.png']
        endpoint.local_handoff = 'b' * 32
        endpoint.cancel = MagicMock()
        endpoint.swallow('c' * 32)
        endpoint.cancel.assert_not_called()
        endpoint.swallow('b' * 32)
        endpoint.cancel.assert_called_once_with('a' * 32)

    def test_duplicate_remote_arm_does_not_replace_active_drag(self):
        bridge = MagicMock()
        active = {'id': 'a' * 32, 'crossed': True}
        bridge.context = active
        bridge.controls_input.return_value = True
        DragBridge.service_event(bridge, {'kind': 'drag-arm', 'drag_id': 'b' * 32})
        self.assertIs(bridge.context, active)
        bridge.cancel_drag.assert_called_once_with('b' * 32, notify=True)
        bridge.engine.arm_file_drag.assert_not_called()

    def test_focus_return_completes_drop_without_cancelling_a_released_transfer(self):
        bridge = MagicMock()
        source, target, drag_id = 'a' * 32, 'b' * 32, 'c' * 32
        bridge.context = {'id': drag_id, 'source': source, 'target': target,
                          'crossed': True, 'released': True}
        event = {'event': 'focus', 'from': screen_name(target), 'to': screen_name(source)}
        DragBridge.input_event(bridge, event)
        bridge.clear_context.assert_called_once_with(drag_id)
        bridge.cancel_drag.assert_not_called()

    def test_focus_return_keeps_the_gesture_only_while_left_is_held(self):
        bridge = MagicMock()
        source, target, drag_id = 'a' * 32, 'b' * 32, 'c' * 32
        bridge.context = {'id': drag_id, 'source': source, 'target': target,
                          'crossed': True, 'released': False}
        event = {'event': 'focus', 'from': screen_name(target), 'to': screen_name(source), 'buttons': 2}
        DragBridge.input_event(bridge, event)
        bridge.return_to_source.assert_called_once_with(bridge.context)
        bridge.clear_context.assert_not_called()

    def test_release_waits_for_remote_resume_acknowledgment(self):
        bridge = MagicMock()
        bridge.resumed_local = None
        peer, drag_id, delivery = 'a' * 32, 'b' * 32, Future()
        bridge.resume_delivery = (peer, drag_id, delivery)
        DragBridge.release_resumed(bridge)
        self.assertIsNone(bridge.resume_delivery)
        bridge.service.io_pool.submit.assert_not_called()
        future, after_delivery = bridge.watch.call_args.args
        self.assertIs(future, delivery)
        delivery.set_result({})
        after_delivery(delivery.result())
        bridge.service.io_pool.submit.assert_called_once_with(
            bridge.service.drags.send, peer, 'return-up', {'id': drag_id})
