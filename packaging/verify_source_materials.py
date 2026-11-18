#!/usr/bin/env python3
"""Verify downloaded third-party source materials without extracting or building."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'third_party/SOURCE-MATERIALS.json'


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    base = args.directory.resolve(strict=True)
    lock = json.loads(MANIFEST.read_text())
    for item in lock['archives']:
        path = (base / item['file']).resolve(strict=True)
        if not path.is_relative_to(base) or digest(path) != item['sha256']:
            parser.error('Unexpected path or SHA-256: ' + item['file'])
        print(item['file'] + ': OK')
    provenance = json.loads((ROOT / 'native/drag/macos/vendor.json').read_text())
    if digest(ROOT / 'native/drag/macos/Drag.swift') != provenance['sha256']:
        parser.error('TwinDesk source differs from the declared verbatim file; review and document any changes.')
    license_hash = next(item['sha256'] for item in lock['archives'] if item['component'] == 'TwinDesk GPLv3 text')
    if digest(ROOT / 'third_party/licenses/TwinDesk/LICENSE.txt') != license_hash:
        parser.error('TwinDesk license text does not match the pinned copy')
    print('Bundled TwinDesk source and license: OK')


if __name__ == '__main__':
    main()
