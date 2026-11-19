import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from inputbridge.drag_transfer import commit_native_destination
from inputbridge.files import TransferError
from inputbridge.security import request_json
from inputbridge.service import Service


class DragTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a, self.b = Service(self.root/'a'), Service(self.root/'b')
        for s in [self.a, self.b]:
            s.start('127.0.0.1', 0, '127.0.0.1', polling=False)
        self.b.pair(self.a.create_invite())
        self.folder = self.root / '拖放 文件夹 📁'
        self.folder.mkdir()
        (self.folder/'空目录').mkdir()
        (self.folder/'data.bin').write_bytes(bytes(range(256))*8192)
        (self.folder/'空文件').touch()

    def tearDown(self):
        self.a.stop()
        self.b.stop()
        self.temp.cleanup()

    def offer(self):
        ident = uuid.uuid4().hex
        manifest = self.a.drags.prepare(self.b.device_id, [self.folder], ident)
        return ident, manifest

    def test_drag_uses_independent_tls_session_and_preserves_new_local_clipboard(self):
        ident, _ = self.offer()
        self.a.drags.send(self.b.device_id, 'begin', {'id': ident})
        future = self.b.work_pool.submit(self.b.drags.receive, ident)
        self.b.clipboard_changed(None)
        paths = future.result(timeout=10)
        self.assertEqual((paths[0]/'data.bin').read_bytes(), (self.folder/'data.bin').read_bytes())
        self.assertTrue((paths[0]/'空目录').is_dir())
        self.assertEqual((paths[0]/'空文件').read_bytes(), b'')
        self.assertIsNone(self.b.offer)

    def test_third_paired_peer_cannot_download_or_start_someone_elses_drag(self):
        ident, manifest = self.offer()
        other = Service(self.root/'other')
        other.start('127.0.0.1', 0, '127.0.0.1', polling=False)
        try:
            other.pair(self.a.create_invite())
            peer = other.peers[self.a.device_id]
            item = next(e for e in manifest['entries'] if e['kind'] == 'file')
            with self.assertRaises(TransferError):
                request_json(peer, '/v2/drag/file/%s/%s' % (manifest['id'], item['id']),
                    token=peer['outgoing_token'], local_id=other.device_id)
            with self.assertRaises(TransferError):
                self.b.drags.accept(other.device_id, 'begin', {'id': ident})
        finally:
            other.stop()

    def test_drop_destination_is_exclusive_on_supported_platforms(self):
        ident, _ = self.offer()
        source = self.b.drags.receive(ident)[0]
        destination = self.root/'chosen by native target'
        if sys.platform not in ('darwin', 'linux'):
            with self.assertRaisesRegex(TransferError, '尚未实现'):
                commit_native_destination(source, destination, self.b.profile/'cache')
            self.assertFalse(destination.exists())
            self.assertEqual(list(self.root.glob('.inputbridge-*')), [])
            return
        commit_native_destination(source, destination, self.b.profile/'cache')
        self.assertEqual((destination/'data.bin').read_bytes(), (self.folder/'data.bin').read_bytes())
        (destination/'data.bin').write_bytes(b'keep this')
        with self.assertRaises(FileExistsError):
            commit_native_destination(source, destination, self.b.profile/'cache')
        self.assertEqual((destination/'data.bin').read_bytes(), b'keep this')
        self.assertEqual(list(self.root.glob('.inputbridge-*')), [])
        self.assertTrue(self.folder.exists())

    def test_native_destination_cannot_read_arbitrary_source_paths(self):
        with self.assertRaises(TransferError):
            commit_native_destination(self.folder, self.root/'out', self.b.profile/'cache')

    def test_pause_and_revoke_remove_drag_authorization(self):
        ident, manifest = self.offer()
        self.a.set_paused(True)
        self.assertIsNone(self.a.drags.source_offer(manifest['id'], self.b.device_id))
        self.b.remove_peer(self.a.device_id)
        self.assertNotIn(ident, self.b.drags.incoming)

    def test_protocol_diagnostics_identify_failed_stage_without_private_data(self):
        peer = self.a.peers[self.b.device_id]
        with self.assertRaisesRegex(TransferError, 'session-missing'):
            self.a.drags.send(self.b.device_id, 'begin', {'id': uuid.uuid4().hex})
        with self.assertRaisesRegex(TransferError, 'auth-invalid'):
            request_json(peer, '/v2/drag/offer', {'id': uuid.uuid4().hex},
                         token='0'*64, local_id=self.a.device_id)
        trace = (self.b.profile/'drag-diagnostics.jsonl').read_text()
        self.assertIn('session-missing', trace)
        self.assertIn('auth-invalid', trace)
        self.assertNotIn(peer['outgoing_token'], trace)
        self.assertNotIn(str(self.folder), trace)

    def test_receiver_finish_revokes_source_session_and_late_download(self):
        ident, manifest = self.offer()
        self.b.drags.send(self.a.device_id, 'finish', {'id': ident})
        self.assertNotIn(ident, self.a.drags.outgoing)
        self.assertIsNone(self.a.drags.source_offer(manifest['id'], self.b.device_id))

    def test_release_before_begin_is_retained_and_receiver_can_cancel(self):
        ident, _ = self.offer()
        self.a.drags.send(self.b.device_id, 'button-up', {'id': ident})
        self.a.drags.send(self.b.device_id, 'begin', {'id': ident})
        self.assertTrue(self.b.drags.incoming[ident]['released'])
        self.b.drags.send(self.a.device_id, 'cancel', {'id': ident})
        self.assertNotIn(ident, self.a.drags.outgoing)
