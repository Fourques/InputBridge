"""Own input-core lifecycle and configuration; no installed Deskflow dependency."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QSettings, Signal

from . import scroll
from .files import TransferError
from .security import atomic_json

CORE_PORT = 24830
ID = re.compile(r'^[0-9a-f]{32}$')


def screen_name(device_id):
    if not isinstance(device_id, str) or not ID.fullmatch(device_id):
        raise TransferError('设备 ID 无效')
    return 'ib-' + device_id


def validate_layout(value, local_id, peer_ids):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise TransferError('布局版本无效')
    positions = value.get('positions')
    if not isinstance(positions, dict) or not 2 <= len(positions) <= 8:
        raise TransferError('请选择要连接的电脑')
    if local_id not in positions or value.get('controller_id') not in positions:
        raise TransferError('布局必须包含本机和键鼠所在电脑')
    occupied = set()
    for device_id, point in positions.items():
        screen_name(device_id)
        if device_id not in {local_id, *peer_ids}:
            raise TransferError('布局包含未配对设备')
        if (not isinstance(point, list) or len(point) != 2 or
                any(type(n) is not int or not -4 <= n <= 4 for n in point)):
            raise TransferError('设备位置无效')
        if tuple(point) in occupied:
            raise TransferError('两台电脑不能占用同一位置')
        occupied.add(tuple(point))
    reached = {tuple(positions[local_id])}
    while True:
        adjacent = {p for p in occupied if any(abs(p[0]-q[0]) + abs(p[1]-q[1]) == 1 for q in reached)}
        if adjacent <= reached:
            break
        reached |= adjacent
    if reached != occupied:
        raise TransferError('请将屏幕相邻摆放')
    return {'version': 1, 'controller_id': value['controller_id'], 'positions': positions}


def render_server_config(layout):
    positions = layout['positions']
    by_point = {tuple(point): device_id for device_id, point in positions.items()}
    lines = ['section: links']
    for device_id, (x, y) in positions.items():
        lines.append('  ' + screen_name(device_id) + ':')
        for side, dx, dy in [('left',-1,0),('right',1,0),('up',0,-1),('down',0,1)]:
            other = by_point.get((x+dx, y+dy))
            if other:
                lines.append('    ' + side + ' = ' + screen_name(other))
    return '\n'.join(lines + ['end', ''])


def find_core():
    name = 'inputbridge-core.exe' if sys.platform == 'win32' else 'inputbridge-core'
    if getattr(sys, 'frozen', False):
        root = Path(sys._MEIPASS)
        choices = [root / 'engine' / name]
    else:
        root = Path(__file__).resolve().parents[1]
        choices = [root / 'local' / 'core-build' / 'bin' / name, root / 'local' / 'engine' / name]
    return next((p for p in choices if p.is_file()), None)


def mac_drag_helper():
    root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1] / 'local'
    return root / 'engine/inputbridge-drag-macos'


def core_environment(binary):
    environment = QProcessEnvironment.systemEnvironment()
    runtime = binary.parent / 'qt'
    if not runtime.exists():
        return environment
    try:
        metadata = json.loads((runtime / 'inputbridge-engine-qt.json').read_text())
        files = metadata['runtime_files']
        if not files or any(not (runtime / name).is_file() for name in files):
            raise ValueError('incomplete runtime')
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise TransferError('内置输入核心的 Qt 组件不完整，请重新安装') from error

    for name in environment.keys():
        if name.startswith('DYLD_') or name in {
            'LD_PRELOAD', 'LD_AUDIT', 'QT_QPA_GENERIC_PLUGINS', 'QT_QPA_PLATFORMTHEME',
            'QT_STYLE_OVERRIDE',
        }:
            environment.remove(name)
    plugins = runtime / 'plugins'
    environment.insert('QT_PLUGIN_PATH', str(plugins))
    environment.insert('QT_QPA_PLATFORM_PLUGIN_PATH', str(plugins / 'platforms'))
    return environment


def write_core_settings(service, layout):
    """Derive native settings and pins solely from this app's paired identity."""
    layout = validate_layout(layout, service.device_id, service.peers)
    folder = service.profile.resolve() / 'engine'
    folder.mkdir(mode=0o700, exist_ok=True)
    tls = folder / 'tls'
    tls.mkdir(mode=0o700, exist_ok=True)
    pem = folder / 'identity.pem'
    pem.write_bytes((service.profile / 'certificate.pem').read_bytes() +
                    (service.profile / 'private-key.pem').read_bytes())
    os.chmod(pem, 0o600)
    selected = [service.peers[i] for i in layout['positions'] if i != service.device_id]
    pins = ''.join('v2:sha256:' + p['fingerprint'] + '\n' for p in selected)
    for name in ['trusted-servers', 'trusted-clients']:
        path = tls / name
        path.write_text(pins)
        os.chmod(path, 0o600)
    controller = layout['controller_id']
    mode = 'server' if controller == service.device_id else 'client'
    config = folder / 'layout.conf'
    config.write_text(render_server_config(layout))
    os.chmod(config, 0o600)
    path = folder / 'core.ini'
    settings = QSettings(str(path), QSettings.IniFormat)
    settings.clear()
    values = {
        'core/computerName': screen_name(service.device_id), 'core/port': CORE_PORT,
        'core/interface': urlsplit(service.endpoint).hostname,
        'security/tlsEnabled': True, 'security/checkPeerFingerprints': True,
        'security/certificate': str(pem), 'log/level': 'INFO',
        'server/externalConfig': True, 'server/externalConfigFile': str(config),
        'server/enableClipboard': True, 'server/protocol': 'synergy',
        'server/enableSwitchDelay': False, 'server/enableSwitchDoubleTap': False,
        'client/languageSync': False,
    }
    if mode == 'client':
        values['client/remoteHost'] = urlsplit(service.peers[controller]['endpoint']).hostname
    for key, value in values.items():
        settings.setValue(key, value)
    for device_id in layout['positions']:
        name = screen_name(device_id)
        settings.setValue('computer_' + name + '/name', name)
    settings.sync()
    if settings.status() != QSettings.NoError:
        raise OSError('无法写入输入核心配置')
    os.chmod(path, 0o600)
    return mode, path


class InputEngine(QObject):
    status_changed = Signal(str)
    native_event = Signal(object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_events)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.finished.connect(lambda *_: self.status_changed.emit('键鼠已停止'))
        self.process.errorOccurred.connect(lambda _: self.status_changed.emit('输入核心启动失败'))
        self.pending = b''
        self.path = service.profile / 'input-layout.json'
        self.enabled_path = service.profile / 'input-enabled.json'

    def layout(self):
        if not self.path.exists():
            return None
        return validate_layout(json.loads(self.path.read_text()), self.service.device_id, self.service.peers)

    def save(self, value):
        checked = validate_layout(value, self.service.device_id, self.service.peers)
        atomic_json(self.path, checked)
        write_core_settings(self.service, checked)
        self.status_changed.emit('布局已保存')

    def revoke_removed_peers(self):
        if not self.path.exists():
            return
        value = json.loads(self.path.read_text())
        if any(i != self.service.device_id and i not in self.service.peers for i in value['positions']):
            self.stop(disable=True)
            self.path.unlink()
            for name in ['trusted-servers', 'trusted-clients']:
                p = self.service.profile / 'engine' / 'tls' / name
                if p.exists():
                    p.write_text('')

    def start(self):
        binary = find_core()
        if binary is None:
            raise TransferError('内置输入核心尚未构建')
        layout = self.layout()
        if layout is None:
            raise TransferError('请先设置屏幕布局')
        if sys.platform == 'darwin':
            helper = mac_drag_helper()
            capabilities = json.loads(subprocess.check_output([str(helper), '--capabilities'], text=True, timeout=3))
            if not capabilities['accessibilityGranted']:
                raise PermissionError('需要辅助功能权限')
        if self.process.state() != QProcess.NotRunning:
            raise TransferError('请先停止当前输入核心')
        if sys.platform != 'win32' and subprocess.run(
                ['pgrep', '-x', 'deskflow-core'], capture_output=True).returncode == 0:
            raise TransferError('现有键鼠服务仍在运行，请完成切换后启用')
        environment = core_environment(binary)
        mode, settings = write_core_settings(self.service, layout)
        environment.insert('INPUTBRIDGE_EVENTS', '1')
        environment.insert('INPUTBRIDGE_ENGINE_HOME', str(settings.parent))
        if sys.platform == 'darwin':
            bits, _ = self.wheel_policy(scroll.load(self.service.profile))
            environment.insert('INPUTBRIDGE_WHEEL_OPTIONS', str(bits))
        self.process.setProcessEnvironment(environment)
        self.process.setProgram(str(binary))
        self.process.setArguments([mode, '--settings', str(settings)])
        self.process.start()
        atomic_json(self.enabled_path, {'enabled': True})
        self.status_changed.emit('正在启动键鼠')

    @staticmethod
    def wheel_policy(value):
        bits = scroll.encode(value)
        if not bits & 15:
            return bits, False
        try:
            # Avoid competing with another global scroll filter during trial.
            mos = subprocess.run(['/usr/bin/pgrep', '-x', 'Mos'], capture_output=True, timeout=1)
            if mos.returncode == 1:
                return bits, False
        except (OSError, subprocess.TimeoutExpired):
            pass
        return scroll.encode(value, local=False), True

    def save_wheel(self, value):
        settings = scroll.save(self.service.profile, value)
        bits, blocked = self.wheel_policy(settings)
        if self.process.state() != QProcess.Running:
            return '滚轮设置已保存' if not blocked else '已保存，退出 Mos 后再启用键鼠'
        from PySide6.QtNetwork import QLocalSocket
        socket = QLocalSocket()
        socket.connectToServer('inputbridge-core')
        try:
            if socket.waitForConnected(150):
                socket.write(('wheel=%d\n' % bits).encode('ascii'))
                socket.flush()
                if socket.bytesAvailable() or socket.waitForReadyRead(180):
                    if b'ok' in bytes(socket.readAll()).splitlines():
                        return '已应用' if not blocked else '退出 Mos，再点一次应用'
        finally:
            socket.disconnectFromServer()
        return '已保存，重新启用键鼠后生效'

    def restore(self):
        if self.enabled_path.exists() and json.loads(self.enabled_path.read_text()).get('enabled'):
            try:
                self.start()
            except (OSError, TransferError) as error:
                self.status_changed.emit(str(error))

    def stop(self, disable=False):
        if disable:
            atomic_json(self.enabled_path, {'enabled': False})
        if self.process.state() != QProcess.NotRunning:
            from PySide6.QtNetwork import QLocalSocket
            socket = QLocalSocket(self)
            socket.connectToServer('inputbridge-core')
            if socket.waitForConnected(100):
                socket.write(b'stop\n')
                socket.waitForBytesWritten(100)
                socket.disconnectFromServer()
            else:
                self.process.terminate()
            socket.deleteLater()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)

    def arm_file_drag(self, enabled=True):
        from PySide6.QtNetwork import QLocalSocket
        if self.process.state() != QProcess.Running:
            raise TransferError('输入核心尚未运行')
        socket = QLocalSocket(self)
        socket.connectToServer('inputbridge-core')
        if not socket.waitForConnected(100):
            socket.deleteLater()
            raise TransferError('无法连接输入核心')
        socket.write(b'file-drag=' + (b'1' if enabled else b'0') + b'\n')
        socket.waitForBytesWritten(100)
        socket.disconnectFromServer()
        socket.deleteLater()

    def read_events(self):
        raw = bytes(self.process.readAllStandardOutput())
        self.pending += raw
        if len(self.pending) > 65536:
            self.pending = b''
            return
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            if line.startswith(b'IB_EVENT ') and len(line) <= 8192:
                try:
                    data = json.loads(line[9:])
                except (ValueError, UnicodeError):
                    continue
                if isinstance(data, dict) and data.get('event') in ('focus', 'button-up'):
                    if data['event'] == 'focus':
                        self.service.drags.trace('input-focus', code=
                            'local' if data.get('to') == screen_name(self.service.device_id) else 'peer')
                    elif data.get('button') == 1:
                        self.service.drags.trace('physical-release', code='left')
                    self.native_event.emit(data)
