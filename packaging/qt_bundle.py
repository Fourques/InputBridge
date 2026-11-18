"""Copy the Qt dependency closure used by the standalone keyboard/mouse engine."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def output(*command):
    return subprocess.check_output([str(arg) for arg in command], text=True)


def run(*command):
    subprocess.run([str(arg) for arg in command], check=True)


def mac_dependencies(path):
    report = output('otool', '-L', path).splitlines()
    return [line.strip().split(' (compatibility version', 1)[0] for line in report[1:]]


def mac_rpaths(path):
    report = output('otool', '-l', path).splitlines()
    return [report[index + 2].strip().removeprefix('path ').rsplit(' (offset ', 1)[0]
            for index, line in enumerate(report) if line.strip() == 'cmd LC_RPATH']


def build_runtime(qt, native, core, mode, patchelf):
    """Bind each Qt load to a specific sibling file before packaging."""
    is_mac = mode == 'macos'
    root = native / 'qt'
    (root / 'lib').mkdir(parents=True)
    plugins = (('libqcocoa.dylib', 'libqoffscreen.dylib') if is_mac
               else ('libqxcbInputBridge.so', 'libqoffscreenInputBridge.so'))
    platforms = root / 'plugins/platforms'
    platforms.mkdir(parents=True)
    queue = [core]
    for name in plugins:
        queue.append(Path(shutil.copy2(qt / 'plugins/platforms' / name, platforms / name)))
    copied = set()
    for binary in queue:
        if is_mac:
            libraries = mac_dependencies(binary)
            if binary.parent == root / 'lib':
                libraries = libraries[1:]  # The first entry is this dylib's install ID.
            for value in mac_rpaths(binary):
                run('install_name_tool', '-delete_rpath', value, binary)
        else:
            libraries = output(patchelf, '--print-needed', binary).splitlines()
            relative_libs = os.path.relpath(root / 'lib', binary.parent)
            run(patchelf, '--force-rpath', '--set-rpath', '$ORIGIN/' + relative_libs, binary)
        for library in libraries:
            name = Path(library).name
            if not name.startswith('libQt6'):
                continue
            target = root / 'lib' / name
            if target not in copied:
                shutil.copy2(qt / 'lib' / name, target)
                copied.add(target)
                queue.append(target)
            if is_mac:
                relative = os.path.relpath(target, binary.parent)
                run('install_name_tool', '-change', library, '@loader_path/' + relative, binary)
            elif not name.endswith('InputBridge.so.6'):
                raise RuntimeError('Linux core is linked to a standard Qt soname: ' + name)

    metadata = json.loads((qt / 'inputbridge-engine-qt.json').read_text())
    metadata.pop('toolchain_arguments', None)
    metadata['runtime_files'] = sorted(str(path.relative_to(root)) for path in queue[1:])
    (root / 'inputbridge-engine-qt.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (native / 'qt.conf').write_text('[Paths]\nPrefix=qt\nLibraries=lib\nPlugins=plugins\n')
    if is_mac:
        for binary in queue:
            run('codesign', '--force', '--sign', '-', binary)
