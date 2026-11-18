#!/usr/bin/env python3
"""Compare a QtCore binary with the pinned Qt source's Tika MIME data."""
import argparse
import ctypes
import ctypes.util
import hashlib
import json
from pathlib import Path
import tarfile
import zlib

TIKA_SOURCE = 'src/corelib/mimetypes/3rdparty/tika-mimetypes.xml'


def occurrences(payload, token):
    start = 0
    while (found := payload.find(token, start)) != -1:
        yield found
        start = found + 1


def zstd_library(name):
    lib = ctypes.CDLL(name)
    lib.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    return lib


def compare(binary, xml, zstd):
    matches = []
    if xml in binary:
        matches.append('plain')
    for start in occurrences(binary, b'\x1f\x8b\x08'):
        try:
            data = zlib.decompressobj(31).decompress(binary[start:], len(xml) + 1)
        except zlib.error:
            continue
        if data == xml:
            matches.append('gzip')
    if zstd:
        for start in occurrences(binary, b'\x28\xb5\x2f\xfd'):
            raw = binary[start:]
            size = zstd.ZSTD_findFrameCompressedSize(raw, len(raw))
            if zstd.ZSTD_isError(size):
                continue
            out = ctypes.create_string_buffer(len(xml) + 1)
            actual = zstd.ZSTD_decompress(out, len(out), raw, size)
            if actual == len(xml) and out.raw[:actual] == xml:
                matches.append('zstd')
    return matches


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('qtcore', type=Path)
    p.add_argument('qtbase_archive', type=Path)
    p.add_argument('--zstd-library', help='Optional path to an installed libzstd')
    p.add_argument('--require-absent', action='store_true', help='Fail if a payload matches or zstd was not tested')
    args = p.parse_args()
    with tarfile.open(args.qtbase_archive) as archive:
        matches = [f for f in archive if f.isfile() and f.name.endswith('/' + TIKA_SOURCE)]
        if len(matches) != 1 or not 0 < matches[0].size < 2_000_000:
            p.error('Expected one versioned Tika source file')
        xml = archive.extractfile(matches[0]).read()
    libname = args.zstd_library or ctypes.util.find_library('zstd')
    lib = zstd_library(libname) if libname else None
    binary = args.qtcore.read_bytes()
    formats = compare(binary, xml, lib)
    print(json.dumps({
        'qtcore_sha256': hashlib.sha256(binary).hexdigest(),
        'source_sha256': hashlib.sha256(xml).hexdigest(),
        'source_bytes': len(xml),
        'exact_payload_formats': formats,
        'zstd_checked': lib is not None,
        'scope': 'Exact payload comparison only. No match is not proof that modified data is absent.',
    }, indent=2))
    if args.require_absent and (formats or lib is None):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
