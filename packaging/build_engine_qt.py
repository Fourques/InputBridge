#!/usr/bin/env python3
"""Build the core's isolated QtBase SDK from the pinned upstream source archive."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = '6.7.3'


def digest(path):
    with path.open('rb') as stream:
        h = hashlib.sha256()
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def run(*command):
    subprocess.run([str(v) for v in command], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--source', required=True, type=Path, help='Official QtBase 6.7.3 source archive')
    p.add_argument('--out', required=True, type=Path, help='New build directory')
    p.add_argument('--cmake', default='cmake')
    p.add_argument('--ninja', default='ninja')
    p.add_argument('--jobs', type=int, default=3)
    p.add_argument('--cmake-arg', action='append', default=[], help='Additional host toolchain option')
    a = p.parse_args()
    if sys.platform not in ('darwin', 'linux') or a.jobs < 1:
        p.error('Requires native macOS/Linux and at least one job')
    archive = a.source.resolve(strict=True)
    lock = json.loads((ROOT/'third_party/SOURCE-MATERIALS.json').read_text())
    expected = next(x['sha256'] for x in lock['archives'] if x['component'] == 'QtBase')
    if digest(archive) != expected:
        p.error('QtBase source does not match the pinned SHA-256')
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    run('tar', '-xf', archive, '-C', out)
    source = out/f'qtbase-everywhere-src-{VERSION}'
    patches = []
    sdk = out/'sdk'
    build = out/'build'
    flags = [
        '-DFEATURE_mimetype_database=OFF', '-DQT_BUILD_EXAMPLES=OFF',
        '-DQT_BUILD_TESTS=OFF', '-DQT_BUILD_BENCHMARKS=OFF',
        '-DFEATURE_sql=OFF', '-DFEATURE_printsupport=OFF', '-DFEATURE_opengl=OFF',
    ]
    if sys.platform == 'linux':
        # Distinct ELF sonames keep the core's symbol versions and prevent the
        # loader from substituting the ordinary system or PySide Qt libraries.
        flags.extend(['-DFEATURE_xcb=ON', '-DFEATURE_vulkan=OFF', '-DQT_LIBINFIX=InputBridge'])
    if sys.platform == 'darwin':
        # Use flat shared libraries for the private child runtime; the main
        # PySide application continues to use its existing frameworks.
        flags.append('-DFEATURE_framework=OFF')
        if platform.machine() == 'arm64':
            # Recent Apple Clang requires the ACLE declaration for __yield(),
            # including when Qt's configure probes compile from C.
            path = source/'src/corelib/thread/qyieldcpu.h'
            original = path.read_text()
            anchor = '#include <QtCore/qtconfigmacros.h>\n'
            if original.count(anchor) != 1:
                raise RuntimeError('Unexpected Qt ARM compatibility patch context')
            path.write_text(original.replace(anchor, anchor + '\n#include <arm_acle.h>\n'))
            patches.append('src/corelib/thread/qyieldcpu.h: include <arm_acle.h>')
    run(a.cmake, '-S', source, '-B', build, '-G', 'Ninja',
        '-DCMAKE_MAKE_PROGRAM='+str(Path(a.ninja).resolve() if '/' in a.ninja else a.ninja),
        '-DCMAKE_BUILD_TYPE=Release', *a.cmake_arg, *flags,
        '-DCMAKE_INSTALL_PREFIX='+str(sdk))
    cache = (build/'CMakeCache.txt').read_text()
    if 'QT_FEATURE_mimetype_database:INTERNAL=OFF' not in cache:
        raise RuntimeError('Qt did not disable its embedded MIME data')
    run(a.cmake, '--build', build, '--parallel', str(a.jobs))
    run(a.cmake, '--install', build)
    manifest = {
        'qt_version': VERSION, 'source_sha256': expected, 'mimetype_database': False,
        'platform': sys.platform, 'configure_flags': flags,
        'toolchain_arguments': a.cmake_arg, 'source_patches': patches,
        'distribution_approved': False,
    }
    (sdk/'inputbridge-engine-qt.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('Private QtBase SDK:', sdk, flush=True)


if __name__ == '__main__':
    main()
