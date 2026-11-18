"""Authenticated drag sessions, isolated from the general clipboard channel."""
from contextlib import contextmanager
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

from .files import ID, TransferError, build_offer, check_cancel, receive_offer, validate_manifest
from .security import PinnedConnection, request_json


class DragProtocolError(TransferError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class DragTransfers:
    def __init__(self, service):
        self.service = service
        self.outgoing, self.incoming = {}, {}
        self.log = logging.Logger('drag')
        handler = RotatingFileHandler(service.profile / 'drag-diagnostics.jsonl',
                                     maxBytes=256*1024, backupCount=1, encoding='utf-8')
        os.chmod(handler.baseFilename, 0o600)
        self.log.addHandler(handler)

    def trace(self, stage, drag_id=None, action=None, code=None):
        # Callers supply fixed stage/code labels only. Never paths, tokens or input data.
        value = {'time': time.time(), 'stage': stage}
        if isinstance(drag_id, str) and ID.fullmatch(drag_id):
            value['drag'] = drag_id
        if action in ('offer', 'arm', 'begin', 'swallow', 'cancel', 'button-up', 'finish'):
            value['action'] = action
        if code:
            value['code'] = code
        self.log.warning(json.dumps(value))

    def send(self, peer_id, action, data):
        with self.service.lock:
            peer = dict(self.service.peers[peer_id])
        self.trace('send', data.get('id'), action)
        try:
            result = request_json(peer, '/v2/drag/' + action, data,
                                token=peer['outgoing_token'], local_id=self.service.device_id)
        except Exception:
            self.trace('send-failed', data.get('id'), action)
            raise
        self.trace('sent', data.get('id'), action)
        return result

    def prepare(self, peer_id, paths, drag_id):
        if not ID.fullmatch(drag_id) or len(paths) > 64:
            raise DragProtocolError('invalid-id', '拖放请求无效')
        offer = build_offer(paths)
        with self.service.lock:
            if self.service.paused or peer_id not in self.service.peers:
                raise TransferError('文件同步已暂停或设备已移除')
            if len(self.outgoing) >= 8:
                oldest = next(iter(self.outgoing))
                self.outgoing.pop(oldest)
            self.outgoing[drag_id] = {'peer': peer_id, 'offer': offer}
        try:
            self.send(peer_id, 'offer', {'id': drag_id, 'manifest': offer.manifest})
        except Exception:
            with self.service.lock:
                self.outgoing.pop(drag_id, None)
            raise
        return offer.manifest

    def accept(self, sender, action, data):
        if not isinstance(data, dict) or not ID.fullmatch(str(data.get('id', ''))):
            raise DragProtocolError('invalid-id', '拖放请求无效')
        drag_id = data['id']
        with self.service.lock:
            if sender not in self.service.peers or self.service.paused:
                raise DragProtocolError('paused-or-revoked', '设备未授权或同步已暂停')
            if action == 'offer':
                manifest = validate_manifest(data['manifest'])
                if len(manifest['roots']) > 64 or drag_id in self.incoming:
                    raise DragProtocolError('duplicate-or-large', '拖放请求已存在或过大')
                for key, entry in list(self.incoming.items()):
                    if not entry['begun'] and entry['expires'] < time.monotonic():
                        entry['cancel'].set()
                        self.incoming.pop(key)
                if len(self.incoming) >= 8:
                    raise DragProtocolError('busy', '正在处理其它拖放')
                self.incoming[drag_id] = {'peer': sender, 'manifest': manifest,
                    'cancel': threading.Event(), 'begun': False, 'released': False,
                    'expires': time.monotonic()+120}
                return
            outgoing = self.outgoing.get(drag_id)
            incoming = self.incoming.get(drag_id)
            if action in ('swallow', 'finish') or (action == 'cancel' and outgoing):
                if not outgoing or outgoing['peer'] != sender:
                    raise DragProtocolError('source-mismatch', '拖放来源不匹配')
                if action in ('finish', 'cancel'):
                    self.outgoing.pop(drag_id)
            else:
                if not incoming or incoming['peer'] != sender:
                    raise DragProtocolError('session-missing', '拖放来源不匹配')
                if not incoming['begun'] and time.monotonic() > incoming['expires']:
                    raise DragProtocolError('expired', '拖放已过期')
                if action == 'begin':
                    if incoming['begun']:
                        raise DragProtocolError('already-begun', '拖放已经开始')
                    incoming['begun'] = True
                elif action == 'cancel':
                    incoming['cancel'].set()
                    self.incoming.pop(drag_id, None)
                elif action == 'button-up':
                    incoming['released'] = True
                elif action not in ('arm', 'button-up'):
                    raise DragProtocolError('unknown-action', '未知拖放操作')
        self.service.emit('drag-' + action, drag_id=drag_id, peer_id=sender)

    def source_offer(self, offer_id, peer_id):
        for entry in self.outgoing.values():
            offer = entry['offer']
            if (entry['peer'] == peer_id and offer.manifest['id'] == offer_id and
                    time.monotonic() < offer.expires):
                return offer
        return None

    def receive(self, drag_id):
        with self.service.lock:
            entry = self.incoming[drag_id]
            peer = dict(self.service.peers[entry['peer']])
            manifest, cancel = entry['manifest'], entry['cancel']

        @contextmanager
        def fetch(item):
            check_cancel(cancel)
            connection = PinnedConnection(peer, timeout=30)
            try:
                connection.request('GET', '/v2/drag/file/%s/%s' % (manifest['id'], item['id']),
                    headers={'Authorization': 'Bearer ' + peer['outgoing_token'],
                             'X-InputBridge-Device': self.service.device_id})
                response = connection.getresponse()
                if response.status != 200 or response.getheader('Content-Length') != str(item['size']):
                    raise TransferError('拖放源文件已变化或连接中断')
                yield response
            finally:
                connection.close()

        return receive_offer(manifest, fetch, self.service.profile / 'cache', cancel)

    def revoke(self, peer_id=None):
        for key, entry in list(self.incoming.items()):
            if peer_id is None or entry['peer'] == peer_id:
                entry['cancel'].set()
                self.incoming.pop(key, None)
        for key, entry in list(self.outgoing.items()):
            if peer_id is None or entry['peer'] == peer_id:
                self.outgoing.pop(key, None)


def commit_native_destination(source, destination, cache_root):
    """Called only with an OS native drag callback, never a network destination."""
    source, destination = Path(source).resolve(), Path(destination)
    if not source.is_relative_to(Path(cache_root).resolve()) or source.is_symlink():
        raise TransferError('拖放缓存无效')
    if not destination.is_absolute() or destination.name in ('', '.', '..'):
        raise TransferError('原生拖放目标无效')
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    if target.exists() or target.is_symlink():
        raise FileExistsError('目标已存在，未覆盖')
    folder = Path(tempfile.mkdtemp(prefix='.inputbridge-', dir=parent))
    staged = folder / 'payload'
    try:
        if source.is_dir():
            shutil.copytree(source, staged)
        else:
            shutil.copy2(source, staged)
        # Same-volume, exclusive atomic commit: a concurrent existing item is never replaced.
        if sys.platform == 'darwin':
            lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            fn = lib.renamex_np
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            result = fn(os.fsencode(staged), os.fsencode(target), 4)  # RENAME_EXCL
        elif sys.platform.startswith('linux'):
            lib = ctypes.CDLL(None, use_errno=True)
            fn = lib.renameat2
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            result = fn(-100, os.fsencode(staged), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
        else:
            raise TransferError('此系统的原生拖放尚未实现')
        if result:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return target
    finally:
        shutil.rmtree(folder, ignore_errors=True)
