import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings

from inputbridge.engine import core_environment, validate_layout, render_server_config, write_core_settings
from inputbridge.files import TransferError
from inputbridge.service import Service


class CoreRuntimeTests(unittest.TestCase):
    def test_private_runtime_overrides_ui_plugin_paths_and_checks_files(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Path(directory) / 'inputbridge-core'
            private = core.parent / 'qt'
            plugin = private / 'plugins/platforms/platform-plugin'
            plugin.parent.mkdir(parents=True)
            (private / 'inputbridge-engine-qt.json').write_text(
                json.dumps({'runtime_files': ['plugins/platforms/platform-plugin']})
            )
            with self.assertRaises(TransferError):
                core_environment(core)
            plugin.touch()
            with patch.dict(os.environ, {
                'QT_PLUGIN_PATH': '/ui/qt/plugins',
                'QT_QPA_PLATFORM_PLUGIN_PATH': '/ui/qt/plugins/platforms',
                'QT_STYLE_OVERRIDE': 'external',
            }):
                child = core_environment(core)
            self.assertEqual(child.value('QT_PLUGIN_PATH'), str(private / 'plugins'))
            self.assertEqual(child.value('QT_QPA_PLATFORM_PLUGIN_PATH'), str(plugin.parent))
            self.assertFalse(child.contains('QT_STYLE_OVERRIDE'))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.a = Service(Path(self.temp.name) / 'a', name='Mac')
        self.b = Service(Path(self.temp.name) / 'b', name='Linux')
        for s in [self.a, self.b]:
            s.start('127.0.0.1', 0, '127.0.0.1', polling=False)
        self.b.pair(self.a.create_invite())
        self.layout = {'version': 1, 'controller_id': self.a.device_id,
                       'positions': {self.a.device_id: [0, 0], self.b.device_id: [1, 0]}}

    def tearDown(self):
        self.a.stop()
        self.b.stop()
        self.temp.cleanup()

    def test_own_tls_config_and_reciprocal_screen_links(self):
        server, pa = write_core_settings(self.a, self.layout)
        client, pb = write_core_settings(self.b, self.layout)
        self.assertEqual((server, client), ('server', 'client'))
        self.assertTrue(pa.is_relative_to(self.a.profile.resolve()))
        self.assertTrue(pb.is_relative_to(self.b.profile.resolve()))
        settings = QSettings(str(pb), QSettings.IniFormat)
        self.assertEqual(settings.value('client/remoteHost'), '127.0.0.1')
        self.assertTrue(settings.value('security/checkPeerFingerprints', type=bool))
        pins = (pb.parent / 'tls/trusted-servers').read_text()
        self.assertEqual(pins, 'v2:sha256:' + self.a.fingerprint + '\n')
        config = render_server_config(self.layout)
        self.assertIn('right = ib-' + self.b.device_id, config)
        self.assertIn('left = ib-' + self.a.device_id, config)

    def test_authenticated_layout_is_saved_on_both_without_launching_core(self):
        self.a.configure_input(self.layout)
        for s in [self.a, self.b]:
            self.assertEqual(json.loads((s.profile / 'input-layout.json').read_text()), self.layout)

    def test_layout_rejects_unknown_overlap_disconnected_and_config_injection(self):
        cases = []
        for point in [[0, 0], [4, 4], [True, 0], [1.5, 0]]:
            value = copy.deepcopy(self.layout)
            value['positions'][self.b.device_id] = point
            cases.append(value)
        for invalid_id in ['c' * 32, 'bad\nsection: options']:
            value = copy.deepcopy(self.layout)
            value['positions'][invalid_id] = value['positions'].pop(self.b.device_id)
            cases.append(value)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(TransferError):
                validate_layout(case, self.a.device_id, self.a.peers)

    def test_peer_cannot_authorize_third_device_through_layout(self):
        value = copy.deepcopy(self.layout)
        value['positions']['c' * 32] = [0, 1]
        with self.assertRaises(TransferError):
            self.b.accept_input_layout(self.a.device_id, value)
        self.assertFalse((self.b.profile / 'input-layout.json').exists())
