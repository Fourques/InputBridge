import tempfile
import unittest
from pathlib import Path

from inputbridge import scroll


class WheelSettingsTests(unittest.TestCase):
    def test_remote_direction_does_not_enable_local_transform(self):
        settings = scroll.sanitize({'remote_reverse_vertical': True, 'speed': 170, 'duration_ms': 187})
        bits = scroll.encode(settings)
        self.assertEqual(bits & 63, 16)
        self.assertEqual((bits >> 13) & 511, 170)
        self.assertEqual((bits >> 22) & 63, 19)

    def test_mos_coexistence_masks_only_local_adjustments(self):
        bits = scroll.encode({'smooth_vertical': True, 'reverse_horizontal': True,
                              'remote_reverse_vertical': True}, local=False)
        self.assertEqual(bits & 63, 16)

    def test_invalid_profile_falls_back_without_enabling_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / 'scroll.json').write_text('broken')
            self.assertEqual(scroll.encode(scroll.load(profile)) & 63, 0)
            scroll.save(profile, {'speed': 10000, 'smooth_vertical': 1,
                                  'remote_reverse_horizontal': True})
            self.assertEqual(scroll.load(profile)['speed'], 100)
            self.assertEqual(scroll.encode(scroll.load(profile)) & 63, 32)
