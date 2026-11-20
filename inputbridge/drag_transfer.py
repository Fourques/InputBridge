"""Authenticated drag sessions, isolated from the general clipboard channel."""
from contextlib import contextmanager
import ctypes
from dataclasses import replace
from http.client import HTTPException
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid

from .files import ID, TransferError, build_offer, check_cancel, receive_offer, stamp, validate_manifest
from .security import PinnedConnection, request_json


class DragProtocolError(TransferError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class DragTransfers:
    def __init__(self, service):
        self.service = service
        self.outgoing, self.incoming = {}, {}
        self.move_receipts = {}
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
        if action in ('offer', 'arm', 'begin', 'swallow', 'cancel', 'button-up', 'finish',
                      'return', 'return-up', 'commit-move'):
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

    def prepare(self, peer_id, paths, drag_id, preview=None, previous=None):
        if not ID.fullmatch(drag_id) or len(paths) > 64:
            raise DragProtocolError('invalid-id', '拖放请求无效')
        if previous and time.monotonic() < previous.expires:
            # The held selection is unchanged on a return trip. File opens still
            # check the original snapshot, and this trip gets a new transfer ID.
            offer = replace(previous, manifest={**previous.manifest, 'id': uuid.uuid4().hex})
        else:
            offer = build_offer(paths)
        if preview and len(offer.manifest["entries"]) == 1 and offer.manifest["entries"][0]["kind"] == "file":
            offer.manifest["preview_png"] = preview
        with self.service.lock:
            if self.service.paused or peer_id not in self.service.peers:
                raise TransferError('文件同步已暂停或设备已移除')
            if len(self.outgoing) >= 8:
                oldest = next(iter(self.outgoing))
                self.outgoing.pop(oldest)
            self.outgoing[drag_id] = {'peer': peer_id, 'offer': offer}
        try:
            self.send(peer_id, 'offer', {'id': drag_id, 'manifest': offer.manifest, 'move': True})
        except Exception:
            with self.service.lock:
                self.outgoing.pop(drag_id, None)
            raise
        return offer.manifest

    def handed_off(self, drag_id, peer_id):
        with self.service.lock:
            entry = self.outgoing.get(drag_id)
            if entry and entry['peer'] == peer_id and not entry.get('returned'):
                entry['handed_off'] = True

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
                    'expires': time.monotonic()+120, 'move': data.get('move') is True}
                return
            outgoing = self.outgoing.get(drag_id)
            incoming = self.incoming.get(drag_id)
            if action == 'commit-move':
                return self.commit_move(sender, drag_id, data.get('offer'), outgoing)
            if action == 'return-up' and outgoing is None:
                # The native drag can finish before the physical release reaches this peer.
                return
            if action in ('swallow', 'finish', 'return', 'return-up') or (action == 'cancel' and outgoing):
                if not outgoing or outgoing['peer'] != sender:
                    raise DragProtocolError('source-mismatch', '拖放来源不匹配')
                if action == 'return' and not outgoing.get('returned'):
                    outgoing['returned'] = True
                elif action == 'return-up' and not outgoing.get('returned'):
                    raise DragProtocolError('not-returned', '拖放未返回')
                elif action == 'return' or (action == 'swallow' and outgoing.get('returned')):
                    raise DragProtocolError('already-returned', '拖放已返回')
                elif action in ('finish', 'cancel', 'return-up'):
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

    def commit_move(self, sender, drag_id, offer_id, outgoing):
        # The caller holds the service lock. Only a source gesture that actually crossed
        # may be retired, and only by the paired receiver that owns that transfer.
        if not isinstance(offer_id, str) or not ID.fullmatch(offer_id):
            raise DragProtocolError('invalid-offer', '移动回执不完整')
        receipt = (sender, drag_id, offer_id)
        if receipt in self.move_receipts:
            return {'status': self.move_receipts[receipt]}
        if (not outgoing or outgoing['peer'] != sender or not outgoing.get('handed_off')
                or outgoing.get('returned') or outgoing['offer'].manifest['id'] != offer_id
                or time.monotonic() >= outgoing['offer'].expires):
            raise DragProtocolError('move-mismatch', '没有对应的源端拖放，原文件已保留')
        offer = outgoing['offer']
        self.outgoing.pop(drag_id)
        if len(self.move_receipts) >= 128:
            completed = next((key for key, status in self.move_receipts.items() if status != 'pending'), None)
            if completed:
                self.move_receipts.pop(completed)
        self.move_receipts[receipt] = 'pending'
        self.service.work_pool.submit(self.retire_move, receipt, offer)
        return {'status': 'pending'}

    def retire_move(self, receipt, offer):
        peer_id, drag_id, _ = receipt
        try:
            retire_move_sources(offer)
        except (OSError, TransferError) as error:
            status = 'retained'
            self.trace('move-source-retained', drag_id, code=type(error).__name__)
            self.service.emit('error', message='目标已保存，源端有文件未能移入废纸篓')
        else:
            status = 'moved'
            self.service.emit('drag-finish', drag_id=drag_id, peer_id=peer_id)
        with self.service.lock:
            self.move_receipts[receipt] = status

    def confirm_move(self, peer_id, drag_id, offer_id):
        deadline = time.monotonic() + 30
        data = {'id': drag_id, 'offer': offer_id}
        while time.monotonic() < deadline:
            try:
                reply = self.send(peer_id, 'commit-move', data)
            except (OSError, HTTPException):
                time.sleep(.5)
                continue
            if reply.get('status') in ('moved', 'retained'):
                return reply['status']
            if reply.get('status') != 'pending':
                break
            time.sleep(.2)
        return 'unknown'

    def source_offer(self, offer_id, peer_id):
        for entry in self.outgoing.values():
            offer = entry['offer']
            if (not entry.get('returned') and entry['peer'] == peer_id and offer.manifest['id'] == offer_id and
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


def rename_exclusive(source, target):
    # Never allow a concurrent path to be replaced.
    if sys.platform == 'darwin':
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        fn = lib.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = fn(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith('linux'):
        lib = ctypes.CDLL(None, use_errno=True)
        fn = lib.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = fn(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        raise TransferError('此系统的原生拖放尚未实现')
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def snapshot_matches(path, relative, tree):
    info = path.lstat()
    item = tree.get(relative)
    mode = stat.S_IFMT(info.st_mode)
    if not item or item[1:] != (mode, stamp(info)):
        return False
    return mode != stat.S_IFDIR or all(snapshot_matches(child, f'{relative}/{child.name}', tree)
                                       for child in path.iterdir())


def retire_move_sources(offer):
    from PySide6.QtCore import QFile

    roots = [Path(offer.tree[name][0]) for name in offer.manifest['roots']]
    if any(not snapshot_matches(path, path.name, offer.tree) for path in roots):
        raise TransferError('拖动期间源文件发生变化，已保留')
    staged = []
    try:
        for source in roots:
            folder = Path(tempfile.mkdtemp(prefix='.inputbridge-move-', dir=source.parent))
            target = folder / source.name
            staged.append((source, target))
            rename_exclusive(source, target)
            if not snapshot_matches(target, source.name, offer.tree):
                raise TransferError('拖动期间源文件发生变化，已保留')
        for source, target in staged:
            if not QFile(str(target)).moveToTrash():
                raise TransferError('原文件无法移入系统废纸篓')
    finally:
        recovery_error = None
        for source, target in reversed(staged):
            try:
                if target.exists() or target.is_symlink():
                    # Keep it in staging if a new item now owns the original name.
                    rename_exclusive(target, source)
                target.parent.rmdir()
            except OSError as error:
                recovery_error = error
        if recovery_error:
            raise recovery_error


def sync_path(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def commit_native_destination(source, destination, cache_root, cancel=None):
    """Called only with an OS native drag callback, never a network destination."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.resolve().is_relative_to(Path(cache_root).resolve()):
        raise TransferError('拖放缓存无效')
    if not destination.is_absolute() or destination.name in ('', '.', '..'):
        raise TransferError('原生拖放目标无效')
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    if target.exists() or target.is_symlink():
        raise FileExistsError('目标已存在，未覆盖')
    check_cancel(cancel)
    folder = Path(tempfile.mkdtemp(prefix='.inputbridge-', dir=parent))
    staged = folder / 'payload'
    try:
        if source.is_dir():
            shutil.copytree(source, staged)
            for directory, _, names in os.walk(staged, topdown=False):
                for name in names:
                    sync_path(Path(directory) / name)
                sync_path(directory)
        else:
            shutil.copy2(source, staged)
            sync_path(staged)
        check_cancel(cancel)
        rename_exclusive(staged, target)
        sync_path(parent)
        return target
    finally:
        shutil.rmtree(folder, ignore_errors=True)
