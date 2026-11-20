import base64
import copy
from io import BytesIO
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from inputbridge.files import (Cancelled, TransferError, build_offer, receive_offer,
                               validate_manifest)
from inputbridge.security import decode_invite, encode_invite, request_json
from inputbridge.service import SendBudget, Server, Service


class ServerTests(unittest.TestCase):
    def test_loopback_bind_does_not_need_reverse_dns(self):
        with patch('socket.getfqdn', side_effect=AssertionError('Unexpected reverse DNS lookup')):
            with Server(('127.0.0.1', 0), None) as server:
                self.assertEqual(server.server_name, '127.0.0.1')
                self.assertEqual(server.server_port, server.socket.getsockname()[1])
                self.assertGreater(server.server_port, 0)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "中文 数据 📁"
        self.source.mkdir()
        (self.source / "empty").mkdir()
        (self.source / "空文件.txt").write_bytes(b"")
        (self.source / "data.bin").write_bytes(bytes(range(256)) * 1000)
        self.offer = build_offer([self.source])

    def tearDown(self):
        self.temporary.cleanup()

    def fetch(self, entry):
        return self.offer.open_file(entry["id"])

    def test_directory_zero_file_and_unicode_round_trip(self):
        paths = receive_offer(self.offer.manifest, self.fetch, self.root / "cache")
        self.assertTrue((paths[0] / "empty").is_dir())
        for name in ["空文件.txt", "data.bin"]:
            self.assertEqual((paths[0] / name).read_bytes(), (self.source / name).read_bytes())

    def test_reject_traversal_and_absolute_and_windows_reserved_names(self):
        for name in ["../escape", "/escape", "C:/escape", "safe/../../escape", "safe\\escape", "NUL.txt", "bad.", "bad/", "a//b"]:
            value = copy.deepcopy(self.offer.manifest)
            value["entries"][0]["path"] = name
            with self.subTest(name=name), self.assertRaises(TransferError):
                validate_manifest(value)

    def test_reject_duplicate_and_case_collisions(self):
        value = copy.deepcopy(self.offer.manifest)
        duplicate = dict(value["entries"][0])
        duplicate["id"] = "a" * 32
        duplicate["path"] = duplicate["path"].upper()
        value["entries"].append(duplicate)
        with self.assertRaises(TransferError):
            validate_manifest(value)

    def test_reject_missing_parent_directory(self):
        value = copy.deepcopy(self.offer.manifest)
        value["entries"] = value["entries"][1:]
        with self.assertRaises(TransferError):
            validate_manifest(value)

    def test_bad_content_never_commits_and_cleans_staging(self):
        cache = self.root / "cache"
        with self.assertRaises(TransferError):
            receive_offer(self.offer.manifest, lambda e: BytesIO(b"wrong"), cache)
        self.assertEqual(list(cache.iterdir()), [])

    def test_cancel_cleans_staging(self):
        cancel = threading.Event()
        def fetch(entry):
            cancel.set()
            return self.fetch(entry)
        cache = self.root / "cache"
        with self.assertRaises(Cancelled):
            receive_offer(self.offer.manifest, fetch, cache, cancel)
        self.assertEqual(list(cache.iterdir()), [])

    def test_repeated_receive_uses_new_directory(self):
        cache = self.root / "cache"
        first = receive_offer(self.offer.manifest, self.fetch, cache)
        (first[0] / "data.bin").write_bytes(b"user modified existing copy")
        second = receive_offer(self.offer.manifest, self.fetch, cache)
        self.assertNotEqual(first, second)
        self.assertEqual((first[0] / "data.bin").read_bytes(), b"user modified existing copy")
        self.assertEqual((second[0] / "data.bin").read_bytes(), (self.source / "data.bin").read_bytes())

    def test_changed_source_is_rejected(self):
        item = next(e for e in self.offer.manifest["entries"] if e["path"].endswith("data.bin"))
        (self.source / "data.bin").write_bytes(b"modified")
        with self.assertRaises(TransferError):
            self.offer.open_file(item["id"])

    @unittest.skipIf(__import__("os").name == "nt", "Creating a symlink may require Windows privileges")
    def test_source_symlink_is_not_followed(self):
        (self.source / "link").symlink_to(self.root)
        with self.assertRaises(TransferError):
            build_offer([self.source])


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.a = Service(self.root / "a", "电脑 A")
        self.b = Service(self.root / "b", "电脑 B")
        for service in [self.a, self.b]:
            service.start("127.0.0.1", 0, "127.0.0.1", polling=False)

    def tearDown(self):
        self.a.stop()
        self.b.stop()
        self.temporary.cleanup()

    def pair(self):
        self.b.pair(self.a.create_invite())

    def events(self, service):
        items = []
        while True:
            try:
                items.append(service.events.get_nowait())
            except queue.Empty:
                return items

    def wait_ready(self, service):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            event = service.events.get(timeout=5)
            if event["kind"] == "error":
                self.fail(event["message"])
            if event["kind"] == "ready":
                return event
        self.fail("No completed receive event")

    def test_mutual_pairing_and_real_tls_file_transfer(self):
        self.pair()
        self.assertIn(self.b.device_id, self.a.peers)
        self.assertTrue(self.b.poll_peer(self.a.device_id))
        source = self.root / "测试 空格.txt"
        source.write_bytes(b"binary\x00\xff data" * 1024)
        self.a.clipboard_changed([source]).result(timeout=10)
        self.assertTrue(self.b.poll_peer(self.a.device_id))
        event = self.wait_ready(self.b)
        self.assertEqual(Path(event["paths"][0]).read_bytes(), source.read_bytes())
        self.assertIn("generation", event)

    def test_https_file_upload_is_paced_without_corrupting_contents(self):
        self.pair()
        self.b.poll_peer(self.a.device_id)
        self.a.send_budget = SendBudget(1024**2)
        source = self.root / 'paced.bin'
        source.write_bytes(bytes(range(256)) * 8192)
        self.a.clipboard_changed([source]).result(timeout=10)
        started = time.monotonic()
        self.b.poll_peer(self.a.device_id)
        event = self.wait_ready(self.b)
        self.assertGreaterEqual(time.monotonic() - started, 1.8)
        self.assertEqual(Path(event['paths'][0]).read_bytes(), source.read_bytes())

    def test_embedded_clipboard_image_uses_existing_authenticated_file_transport(self):
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5ioAAAAASUVORK5CYII=")
        self.pair()
        self.a.clipboard_changed(image).result(timeout=10)
        self.assertTrue(self.b.poll_peer(self.a.device_id))
        received = self.wait_ready(self.b)
        self.assertTrue(received["image"])
        self.assertEqual(Path(received["paths"][0]).read_bytes(), image)

    def test_invitation_is_one_time_and_expiry_is_enforced(self):
        invite = self.a.create_invite()
        self.b.pair(invite)
        with self.assertRaises(TransferError):
            self.b.pair(invite)
        invite = self.a.create_invite()
        self.a.invite_expiry = time.monotonic() - 1
        with self.assertRaises(TransferError):
            self.b.pair(invite)

    def test_copy_immediately_after_pair_is_not_mistaken_for_old_offer(self):
        self.pair()
        source = self.root / "immediate.txt"
        source.write_bytes(b"new copy after pairing")
        self.a.clipboard_changed([source]).result(timeout=10)
        self.assertTrue(self.b.poll_peer(self.a.device_id))
        self.assertEqual(Path(self.wait_ready(self.b)["paths"][0]).read_bytes(), source.read_bytes())

    def test_legacy_auto_receive_setting_cannot_disable_paste(self):
        self.pair()
        self.b.stop()
        config = self.b.config_path
        settings = json.loads(config.read_text())
        config.write_text(json.dumps(dict(settings, auto_receive=False)))
        self.b = Service(config.parent)
        self.b.start("127.0.0.1", 0, "127.0.0.1", polling=False)
        self.assertTrue(self.b.poll_peer(self.a.device_id))
        source = self.root / "copy.png"
        source.write_bytes(b"file payload")
        self.a.clipboard_changed([source]).result(timeout=10)
        self.assertTrue(self.b.poll_peer(self.a.device_id))
        self.assertEqual(Path(self.wait_ready(self.b)["paths"][0]).read_bytes(), source.read_bytes())

    def test_unpaired_peer_cannot_read_offer(self):
        with self.assertRaises(TransferError):
            request_json(self.a.public_device(), "/v1/offer", token="a" * 64,
                         local_id=self.b.device_id)

    def test_wrong_pin_rejected_before_pairing_secret_is_consumed(self):
        invite = decode_invite(self.a.create_invite())
        invite["device"]["fingerprint"] = "0" * 64
        with self.assertRaisesRegex(TransferError, "证书"):
            self.b.pair(encode_invite(invite))
        self.assertIsNotNone(self.a.invite)
        self.assertFalse(self.a.peers)

    def test_removing_peer_revokes_authorization(self):
        self.pair()
        self.a.remove_peer(self.b.device_id)
        self.assertFalse(self.b.poll_peer(self.a.device_id))

    def test_pause_retires_offer_without_stopping_peer_server(self):
        self.pair()
        source = self.root / "one.txt"
        source.write_text("one")
        self.a.clipboard_changed([source]).result(timeout=10)
        self.a.set_paused(True)
        peer = self.b.peers[self.a.device_id]
        response = request_json(peer, "/v1/offer", token=peer["outgoing_token"], local_id=self.b.device_id)
        self.assertTrue(response["paused"])
        self.assertIsNone(response["offer"])

    def test_old_receive_cannot_publish_after_local_copy(self):
        self.pair()
        source = self.root / "one.txt"
        source.write_text("one")
        self.a.clipboard_changed([source]).result(timeout=10)
        manifest = self.a.offer.manifest
        revision = self.b.revision
        self.b.clipboard_changed(None)  # A newer local clipboard event.
        self.b.receive(self.b.peers[self.a.device_id], manifest, revision, threading.Event(), 0)
        self.assertFalse(any(e["kind"] == "ready" for e in self.events(self.b)))

    def test_settings_and_identity_survive_restart(self):
        self.pair()
        fingerprint = self.a.fingerprint
        profile = self.a.profile
        self.a.stop()
        self.a = Service(profile)
        self.assertEqual(self.a.fingerprint, fingerprint)
        self.assertIn(self.b.device_id, self.a.peers)
        if __import__("os").name != "nt":
            self.assertEqual(self.a.config_path.stat().st_mode & 0o777, 0o600)

    def test_no_automatic_replay_of_previous_offer_after_restart(self):
        self.pair()
        source = self.root / "one.txt"
        source.write_text("one")
        self.a.clipboard_changed([source]).result(timeout=10)
        self.b.seen.clear()  # Simulate a restarted receiver observing an old offer.
        self.b.poll_peer(self.a.device_id)
        time.sleep(.15)
        self.assertFalse(any(e["kind"] == "ready" for e in self.events(self.b)))


if __name__ == "__main__":
    unittest.main()
