#!/usr/bin/env python3
"""Stage reviewable source and build local artifacts. Never installs or publishes."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile

from qt_bundle import build_runtime

PROJECT = Path(__file__).resolve().parents[1]
PIN = json.loads((PROJECT / 'native/engine/upstream.json').read_text())
VERSION = runpy.run_path(str(PROJECT / 'inputbridge/__init__.py'))['__version__']

# Local operational notes and live shortcuts never enter the public source tree.
PUBLIC_FILES = (
    '.gitignore', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml',
    'requirements.lock.txt', 'launch_app.py', '.github/workflows/checks.yml',
    'assets/InputBridge.icns', 'scripts/smoke_two_nodes.py',
    'docs/Installation.md', 'docs/Packaging.md', 'docs/Release.md', 'docs/Scroll.md',
    'docs/Licensing.md',
)
PUBLIC_DIRS = ('inputbridge', 'native', 'packaging', 'tests', 'third_party')
LOCAL_FILES = {'tests/test_cutover_guard.py'}
SKIP = {'__pycache__', '.DS_Store'}
EXTENSIONS = {'.py', '.sh', '.swift', '.h', '.cpp', '.json', '.spec', '.txt', '.md'}


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def source_paths():
    paths = set(PUBLIC_FILES)
    for folder in PUBLIC_DIRS:
        for path in (PROJECT / folder).rglob('*'):
            rel = path.relative_to(PROJECT)
            if not set(rel.parts) & SKIP and path.is_file() and path.suffix in EXTENSIONS:
                paths.add(rel.as_posix())
    return sorted(paths - LOCAL_FILES)


def stage(dest):
    seen = []
    for name in source_paths():
        src = PROJECT / name
        if src.resolve() != src or not src.is_file():
            raise RuntimeError('Missing or linked public source: ' + name)
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        seen.append(name)
    return seen


def clean_archive_metadata(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    return info


def tar_directory(root, destination):
    with tarfile.open(destination, 'w:gz') as archive:
        archive.add(root, arcname=root.name, filter=clean_archive_metadata)


def check_checkout(source):
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != PIN['commit']:
        raise RuntimeError('Deskflow HEAD is not the expected pinned commit: ' + PIN['commit'])


def archive_upstream(source, output):
    check_checkout(source)
    run('git', '-C', source, 'archive', '--format=tar.gz', '--prefix=deskflow/',
        '-o', output, PIN['commit'])


def prepare_core_source(args, out):
    source = out / 'deskflow-checkout'
    run('git', 'clone', '--no-hardlinks', '--no-checkout', '--', args.deskflow_checkout, source)
    run('git', '-C', source, '-c', 'advice.detachedHead=false', 'checkout', '--detach', PIN['commit'])
    run(sys.executable, PROJECT / 'native/engine/prepare.py', source)
    return source


def archive_modified_core(source, output):
    tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode().split('\0')
    generated = json.loads((source / '.inputbridge-patches.json').read_text())
    files = sorted((set(tracked) | set(generated)) - {''})
    with tarfile.open(output, 'w:gz') as archive:
        for name in files:
            rel = Path(name)
            if rel.is_absolute() or '..' in rel.parts:
                raise RuntimeError('Invalid path in core source list')
            archive.add(source / rel, 'deskflow-inputbridge/' + rel.as_posix(), recursive=False,
                        filter=clean_archive_metadata)


def build_core(args, out, source):
    build = out / 'core-build'
    qt = args.qt_prefix
    generator = ['-G', 'Ninja'] if args.ninja else []
    if args.ninja:
        generator.append('-DCMAKE_MAKE_PROGRAM=' + args.ninja)
    platform_flags = ['-DBUILD_OSX_BUNDLE=OFF'] if args.mode == 'macos' else ['-DBUILD_X11_SUPPORT=ON']
    run(args.cmake, '-S', source, '-B', build, *generator, *args.cmake_arg,
        '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_GUI=OFF', '-DBUILD_INSTALLER=OFF', '-DBUILD_TESTS=OFF',
        '-DBUILD_WAYLAND_SUPPORT=OFF', *platform_flags,
        '-DCMAKE_PREFIX_PATH=' + str(qt), '-DCMAKE_SKIP_RPATH=' + ('ON' if args.mode == 'linux' else 'OFF'))
    run(args.cmake, '--build', build, '--target', PIN['target'], '--parallel', args.jobs)
    native = out / 'engine'
    native.mkdir()
    core = native / PIN['output']
    shutil.copy2(build / 'bin' / PIN['output'], core)
    return native, core


def check_binary_paths(binary, *roots):
    content = binary.read_bytes()
    for root in (PROJECT, Path.home(), *roots):
        prefix = (str(root).rstrip('/') + '/').encode()
        if prefix != b'/' and prefix in content:
            raise RuntimeError('Build-machine path in ' + binary.name)


def install_macos_engine(native, app):
    frameworks = app / 'Contents/Frameworks/engine'
    resources = app / 'Contents/Resources/engine'
    shutil.copytree(native, frameworks)
    for name in ('qt.conf', 'qt/inputbridge-engine-qt.json'):
        code_location = frameworks / name
        resource_location = resources / name
        resource_location.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(code_location, resource_location)
        code_location.symlink_to(os.path.relpath(resource_location, code_location.parent))


def macos(args, out, public, native, core, source):
    swift = sorted((public / 'native/drag/macos').glob('*.swift'))
    drag = native / 'inputbridge-drag-macos'
    run('xcrun', 'swiftc', '-O', '-o', drag, *swift)
    check_binary_paths(drag, out, args.qt_prefix)
    env = dict(os.environ, INPUTBRIDGE_PACKAGE_ENGINE_DIR=str(native),
               INPUTBRIDGE_PACKAGE_DESKFLOW_SOURCE=str(source))
    for override in ('INPUTBRIDGE_PACKAGE_APP_NAME', 'INPUTBRIDGE_PACKAGE_BUNDLE_ID'):
        env.pop(override, None)
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', out / 'dist',
        '--workpath', out / 'pyinstaller', public / 'packaging/macos.spec', cwd=public, env=env)
    app = out / 'dist/InputBridge.app'
    run('codesign', '--force', '--sign', '-', drag)
    # PyInstaller sees only the UI Qt. Install the already-relocated native
    # runtime after freezing so its libraries are not rewritten or deduplicated.
    install_macos_engine(native, app)
    run('codesign', '--force', '--sign', '-', app)
    run('codesign', '--verify', '--deep', '--strict', app)
    archive = out / f'InputBridge-{VERSION}-macos-{platform.machine()}-local.zip'
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive)
    return archive


def linux(out, public, native, source):
    root = out / f'InputBridge-{VERSION}-linux-{platform.machine()}'
    shutil.copytree(public, root)
    shutil.copytree(native, root / 'local/engine')
    licenses = root / 'licenses/DeskflowCore'
    shutil.copytree(source / 'LICENSES', licenses)
    shutil.copy2(source / 'LICENSE', licenses)
    shutil.copy2(source / 'LICENSE.InputBridge-MIT.txt', licenses)
    archive = out / (root.name + '-local.tar.gz')
    tar_directory(root, archive)
    return archive


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('mode', choices=('source', 'macos', 'linux'))
    parser.add_argument('--out', type=Path, required=True, help='New scratch output directory')
    parser.add_argument('--deskflow-checkout', type=Path, help='Local git checkout of the pinned Deskflow commit')
    parser.add_argument('--qt-prefix', type=Path, help='Private QtBase SDK from build_engine_qt.py')
    parser.add_argument('--patchelf', default='patchelf', help='Linux ELF relocation tool')
    parser.add_argument('--cmake', default='cmake')
    parser.add_argument('--ninja', help='Path to Ninja; default is the system CMake generator')
    parser.add_argument('--cmake-arg', action='append', default=[], help='Additional configure option, e.g. --cmake-arg=-DNAME=value')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    expected = {'macos': 'darwin', 'linux': 'linux'}.get(args.mode)
    if expected and (sys.platform != expected or not args.deskflow_checkout or not args.qt_prefix):
        parser.error('A native host, --deskflow-checkout and --qt-prefix are required for binary builds')
    if args.deskflow_checkout:
        args.deskflow_checkout = args.deskflow_checkout.resolve(strict=True)
        check_checkout(args.deskflow_checkout)
    if args.qt_prefix:
        args.qt_prefix = args.qt_prefix.resolve(strict=True)
        config = args.qt_prefix / 'lib/cmake/Qt6/Qt6ConfigVersionImpl.cmake'
        if not config.is_file() or 'set(PACKAGE_VERSION "6.7.3")' not in config.read_text():
            parser.error('--qt-prefix must contain the matching Qt 6.7.3 SDK')
        manifest = args.qt_prefix / 'inputbridge-engine-qt.json'
        if not manifest.is_file():
            parser.error('Build the private SDK with packaging/build_engine_qt.py first')
        sdk_info = json.loads(manifest.read_text())
        config_header = args.qt_prefix / 'include/QtCore/6.7.3/QtCore/private/qtcore-config_p.h'
        if (sdk_info.get('qt_version') != '6.7.3' or sdk_info.get('platform') != sys.platform
                or sdk_info.get('mimetype_database') is not False or not config_header.is_file()
                or '#define QT_FEATURE_mimetype_database -1' not in config_header.read_text().splitlines()):
            parser.error('--qt-prefix does not identify a matching private QtBase build')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    public = out / f'InputBridge-{VERSION}-source'
    files = stage(public)
    source_archive = out / (public.name + '.tar.gz')
    tar_directory(public, source_archive)
    artifacts = [source_archive]
    if args.deskflow_checkout:
        archive = out / ('deskflow-' + PIN['commit'][:12] + '-source.tar.gz')
        archive_upstream(args.deskflow_checkout, archive)
        artifacts.append(archive)
        source = prepare_core_source(args, out)
        modified = out / ('deskflow-' + PIN['commit'][:12] + '-inputbridge-source.tar.gz')
        archive_modified_core(source, modified)
        artifacts.append(modified)
    if expected:
        native, core = build_core(args, out, source)
        build_runtime(args.qt_prefix, native, core, args.mode, args.patchelf)
        check_binary_paths(core, out, args.qt_prefix)
        artifacts.append(macos(args, out, public, native, core, source) if args.mode == 'macos'
                         else linux(out, public, native, source))
    (out / 'source-files.txt').write_text('\n'.join(files) + '\n')
    (out / 'SHA256SUMS').write_text(''.join(f'{file_hash(p)}  {p.name}\n' for p in artifacts))
    (out / 'build-info.json').write_text(json.dumps({
        'version': VERSION, 'mode': args.mode, 'host': platform.system(), 'architecture': platform.machine(),
        'python': platform.python_version(), 'qt_sdk': '6.7.3' if expected else None,
        'deskflow_commit': PIN['commit'], 'publishable_binary': False,
    }, indent=2) + '\n')
    print('Local build artifacts:', out)


if __name__ == '__main__':
    main()
