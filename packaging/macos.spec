# Full isolated build: python packaging/build.py macos (see docs/Packaging.md).
from pathlib import Path
from importlib.metadata import distribution
import os
from runpy import run_path
from packaging.version import Version

project = Path(SPECPATH).parent
version = run_path(str(project / 'inputbridge' / '__init__.py'))['__version__']
notices = [(str(project / 'LICENSE'), 'licenses/InputBridge'),
           (str(project / 'THIRD_PARTY_NOTICES.md'), 'licenses/InputBridge'),
           (str(project / 'requirements.lock.txt'), 'licenses/InputBridge'),
           (str(project / 'docs' / 'Licensing.md'), 'licenses/InputBridge')]
third_party = project / 'third_party'
for file in sorted((third_party / 'licenses').rglob('*.txt')):
    notices.append((str(file), 'licenses/' + file.parent.relative_to(third_party / 'licenses').as_posix()))
for name in ('SOURCE-MATERIALS.json', 'Qt-source-attributions.json', 'Qt-source-SPDX.json', 'Qt-runtime-audit.json'):
    notices.append((str(third_party / name), 'licenses/SourceReview'))
for dependency in ['PySide6-Essentials', 'shiboken6', 'cryptography', 'cffi', 'pycparser']:
    package = distribution(dependency)
    for relative in package.files or []:
        if 'license' in str(relative).lower() or 'copying' in str(relative).lower():
            full = package.locate_file(relative)
            if full.is_file():
                notices.append((str(full), 'licenses/' + dependency + '/' + str(relative.parent)))
engine_dir = Path(os.environ.get('INPUTBRIDGE_PACKAGE_ENGINE_DIR', project / 'local' / 'engine'))
core = engine_dir / 'inputbridge-core'
native_drag = engine_dir / 'inputbridge-drag-macos'
if core.exists():
    upstream = project / 'local' / 'upstream' / 'deskflow'
    upstream = Path(os.environ.get('INPUTBRIDGE_PACKAGE_DESKFLOW_SOURCE', upstream))
    for file in [upstream / 'LICENSE', upstream / 'LICENSE.InputBridge-MIT.txt',
                 *sorted((upstream / 'LICENSES').glob('*.txt'))]:
        if not file.is_file():
            continue
        notices.append((str(file), 'licenses/DeskflowCore'))
    notices.append((str(project / 'native' / 'engine' / 'upstream.json'), 'licenses/DeskflowCore'))
    notices.append((str(project / 'native' / 'engine' / 'wheel' / 'LICENSE.txt'), 'licenses/InputBridgeWheel'))
if native_drag.exists():
    notices.append((str(project / 'native' / 'drag' / 'macos' / 'Drag.swift'), 'licenses/TwinDeskDrag'))
    notices.append((str(project / 'native' / 'drag' / 'macos' / 'vendor.json'), 'licenses/TwinDeskDrag'))
a = Analysis([str(project / 'launch_app.py')], pathex=[str(project)],
             binaries=[], datas=notices, hiddenimports=[], hookspath=[],
             hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
# The private runtime is installed after freeze by build.py.
# Direct developer freezes can still use their local development helpers.
if not (engine_dir / 'qt/inputbridge-engine-qt.json').is_file():
    for helper in [core, native_drag]:
        if helper.exists():
            a.binaries.append(('engine/' + helper.name, str(helper), 'BINARY'))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='InputBridge',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='InputBridge')
bundle_name = os.environ.get('INPUTBRIDGE_PACKAGE_APP_NAME', 'InputBridge')
app = BUNDLE(collection, name=bundle_name + '.app', icon=str(project / 'assets' / 'InputBridge.icns'),
             bundle_identifier=os.environ.get('INPUTBRIDGE_PACKAGE_BUNDLE_ID', 'org.inputbridge.desktop'),
             info_plist={'LSUIElement': True, 'NSHighResolutionCapable': True,
                         'CFBundleDisplayName': bundle_name,
                         'CFBundleShortVersionString': Version(version).base_version,
                         'CFBundleVersion': '1',
                         'InputBridgeVersion': version,
                         'NSLocalNetworkUsageDescription': '连接已配对的电脑，安全地复制粘贴文件。'})
