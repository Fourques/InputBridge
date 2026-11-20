"""Connect real edge drags, native input focus, and authenticated file sessions."""
import base64
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal

from .drag_native import LinuxDrag, MacDrag
from .drag_transfer import commit_native_destination
from .engine import screen_name
from .files import Cancelled, TransferError
from .images import MAX_PREVIEW_BYTES, decode_raster, png_bytes


class DragBridge(QObject):
    future_ready = Signal(object, object)

    def __init__(self, window):
        super().__init__(window)
        self.window, self.service, self.engine = window, window.service, window.engine
        self.native = None
        self.peer_id = None
        self.context = None
        self.resume_delivery = None
        self.resumed_local = None
        self.resume_snapshot = None
        self.incoming = {}
        self.future_ready.connect(self.finish_future, Qt.QueuedConnection)
        self.last_arm = 0
        self.engine.process.started.connect(self.start)
        self.engine.process.finished.connect(self.stop)
        self.engine.native_event.connect(self.input_event)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(25)

    def watch(self, future, callback):
        future.add_done_callback(lambda result: self.future_ready.emit(result, callback))

    def finish_future(self, future, callback):
        try:
            callback(future.result())
        except Exception as error:
            self.fail(error)

    def tick(self):
        context = self.context
        if not context:
            return
        now = time.monotonic()
        if now >= context['deadline']:
            self.cancel_drag(context['id'], notify=True)
            self.window.transfer_title.setText('拖放已超时')
        elif (context.get('armed') and not context.get('crossed') and not context.get('released') and
              self.controls_input() and now - self.last_arm > .5):
            try:
                self.engine.arm_file_drag()
                self.last_arm = time.monotonic()
            except (OSError, TransferError):
                pass

    def controls_input(self):
        layout = self.engine.layout()
        return bool(layout and layout['controller_id'] == self.service.device_id)

    def clear_context(self, drag_id):
        if not self.context or self.context['id'] != drag_id:
            return
        self.context = None
        if self.controls_input() and self.engine.process.state() == QProcess.Running:
            try:
                self.engine.arm_file_drag(False)
            except (OSError, TransferError):
                pass

    def cancel_drag(self, drag_id, notify=False):
        self.clear_context(drag_id)
        entry = self.incoming.get(drag_id)
        if entry:
            entry['record']['cancel'].set()
            self.cleanup(drag_id, notify=notify)
        elif notify:
            with self.service.lock:
                record = self.service.drags.incoming.pop(drag_id, None)
                if record:
                    record['cancel'].set()
                outgoing = self.service.drags.outgoing.pop(drag_id, None)
            if record or outgoing:
                self.watch(self.service.io_pool.submit(self.service.drags.send,
                    self.peer_id, 'cancel', {'id': drag_id}), lambda _: None)
        if isinstance(self.native, MacDrag):
            self.native.send({'op': 'cancel', 'drag': drag_id})
        elif self.native:
            self.native.cancel(drag_id)

    def fail(self, error):
        self.service.drags.trace('bridge-failed', (self.context or {}).get('id'),
                                 code=type(error).__name__)
        self.window.apply_event({'kind': 'error', 'message': str(error)})

    def start(self):
        self.stop()
        if self.service.paused:
            return
        layout = self.engine.layout()
        if not layout:
            return
        peers = [i for i in layout['positions'] if i != self.service.device_id]
        if len(peers) != 1:
            return
        self.peer_id = peers[0]
        x, y = layout['positions'][self.service.device_id]
        px, py = layout['positions'][self.peer_id]
        side = 'right' if px > x else 'left' if px < x else 'down' if py > y else 'up'
        try:
            self.native = MacDrag(self) if sys.platform == 'darwin' else LinuxDrag(self)
            self.native.event.connect(self.native_event)
            self.native.start(side)
        except Exception as error:
            self.fail(error)
            self.stop()

    def stop(self, *_):
        if self.context and self.engine.process.state() == QProcess.Running:
            try:
                self.engine.arm_file_drag(False)
            except TransferError:
                pass
        if self.native:
            self.native.stop()
            self.native.deleteLater()
            self.native = None
        self.context = None
        self.resume_delivery = None
        self.resumed_local = None
        self.resume_snapshot = None
        for entry in self.incoming.values():
            entry['record']['cancel'].set()
        self.incoming.clear()

    def source(self, event):
        if not self.native or not self.peer_id or self.context:
            return
        drag_id = event.get('drag')
        self.service.drags.trace('native-edge', drag_id)
        paths = event.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            return
        paths = [str(Path(path).absolute()) for path in paths]
        previous = self.resume_snapshot if self.resumed_local else None
        if previous and paths != [previous.tree[name][0] for name in previous.manifest['roots']]:
            previous = None
        self.context = {'id': drag_id, 'source': self.service.device_id,
                        'target': self.peer_id, 'armed': False, 'released': False,
                        'paths': paths,
                        'deadline': time.monotonic() + 45}
        self.window.transfer.show()
        self.window.transfer_title.setText('准备拖放')
        self.window.transfer_hint.setText('')
        peer_id = self.peer_id

        def prepare():
            preview = None
            if len(paths) == 1 and not previous:
                try:
                    data = png_bytes(decode_raster(paths[0], preview=True))
                    if 0 < len(data) <= MAX_PREVIEW_BYTES:
                        preview = base64.b64encode(data).decode("ascii")
                except OSError:
                    pass  # Preview failure must never prevent moving the original file.
            return self.service.drags.prepare(peer_id, paths, drag_id, preview, previous)

        future = self.service.work_pool.submit(prepare)

        def ready(manifest):
            if not self.context or self.context['id'] != drag_id or self.context.get('released'):
                self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'cancel', {'id': drag_id})
                return
            self.context['armed'] = True
            self.context['manifest'] = manifest
            with self.service.lock:
                record = self.service.drags.outgoing.get(drag_id)
                self.context['snapshot'] = record['offer'] if record else None
            self.context['deadline'] = time.monotonic() + 12
            if self.controls_input():
                self.engine.arm_file_drag()
            else:
                future = self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'arm', {'id': drag_id})
                self.watch(future, lambda _: None)
        self.watch(future, ready)

    def resume_source(self, context):
        drag_id = context['id']
        self.clear_context(drag_id)
        self.resumed_local = drag_id
        self.resume_snapshot = context.get('snapshot')
        self.window.transfer_title.setText('已回到来源屏')
        self.native.resume_local(drag_id, context['paths'], context['manifest'])
        QTimer.singleShot(90000, lambda: self.cancel_drag(drag_id)
                          if self.resumed_local == drag_id else None)

    def return_to_source(self, context):
        drag_id = context['id']
        self.service.drags.trace('input-returned', drag_id)
        if context['source'] == self.service.device_id:
            self.cancel_drag(drag_id, notify=True)
            self.resume_source(context)
        else:
            self.cancel_drag(drag_id)
            peer_id = context['source']
            future = self.service.io_pool.submit(self.service.drags.send, peer_id, 'return', {'id': drag_id})
            self.resume_delivery = (peer_id, drag_id, future)
            self.watch(future, lambda _: None)

    def release_resumed(self):
        if self.resumed_local and self.native:
            self.native.released(self.resumed_local)
        if self.resume_delivery:
            peer_id, drag_id, delivered = self.resume_delivery
            self.resume_delivery = None

            def release(_):
                self.service.io_pool.submit(self.service.drags.send, peer_id, 'return-up', {'id': drag_id})
            self.watch(delivered, release)

    def input_event(self, event):
        released = event['event'] == 'button-up' and event.get('button') == 1
        if released:
            self.release_resumed()
        context = self.context
        if not context:
            return
        drag_id = context['id']
        if (event['event'] == 'focus' and context.get('crossed') and
                event.get('from') == screen_name(context['target']) and
                event.get('to') == screen_name(context['source'])):
            if not context.get('released') and event.get('buttons', 0) & (1 << 1):
                self.return_to_source(context)
            else:
                self.clear_context(drag_id)
            return
        if event['event'] == 'focus' and context.get('armed') and not context.get('crossed'):
            if event.get('from') != screen_name(context['source']) or event.get('to') != screen_name(context['target']):
                return
            if not event.get('buttons', 0) & (1 << 1):
                self.cancel_drag(drag_id, notify=True)
                return
            context['crossed'] = True
            self.resume_delivery = None
            context['deadline'] = time.monotonic() + 90
            self.service.drags.trace('input-crossed', drag_id)
            if context['source'] == self.service.device_id:
                self.service.drags.handed_off(drag_id, self.peer_id)
                self.native.send({'op': 'swallow', 'drag': drag_id}) if isinstance(self.native, MacDrag) else self.native.swallow(drag_id)
                future = self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'begin', {'id': drag_id})
                self.watch(future, lambda _: None)
            else:
                record = self.service.drags.incoming.get(drag_id)
                if record is None or record['cancel'].is_set():
                    self.cancel_drag(drag_id, notify=True)
                    return
                if not record['begun']:
                    record['begun'] = True
                    self.service.emit('drag-begin', drag_id=drag_id, peer_id=self.peer_id)
                future = self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'swallow', {'id': drag_id})
                self.watch(future, lambda _: None)
        elif released:
            context['released'] = True
            self.engine.arm_file_drag(False)
            if not context.get('crossed'):
                self.cancel_drag(drag_id, notify=True)
                return
            if context['target'] != self.service.device_id:
                self.watch(self.service.io_pool.submit(self.service.drags.send,
                    self.peer_id, 'button-up', {'id': drag_id}), lambda _: None)
                self.clear_context(drag_id)
            elif isinstance(self.native, MacDrag):
                self.native.send({'op': 'buttonUp', 'drag': drag_id})

    def service_event(self, event):
        kind, drag_id = event['kind'], event['drag_id']
        if kind == 'drag-arm':
            if not self.controls_input():
                return
            if self.context:
                if self.context['id'] != drag_id:
                    self.cancel_drag(drag_id, notify=True)
                return
            self.context = {'id': drag_id, 'source': event['peer_id'],
                            'target': self.service.device_id, 'armed': True, 'released': False,
                            'deadline': time.monotonic() + 12}
            self.engine.arm_file_drag()
        elif kind == 'drag-begin':
            self.begin(drag_id)
        elif kind == 'drag-button-up' and self.native:
            if isinstance(self.native, MacDrag):
                self.native.send({'op': 'buttonUp', 'drag': drag_id})
            else:
                self.native.released(drag_id)
        elif kind == 'drag-finish':
            if not self.context or self.context['id'] == drag_id:
                self.clear_context(drag_id)
                self.window.transfer_title.setText('拖放完成')
        elif kind == 'drag-return':
            context = self.context
            if (context and context['id'] == drag_id and context['source'] == self.service.device_id
                    and self.native and 'manifest' in context):
                self.resume_source(context)
        elif kind == 'drag-return-up':
            if self.resumed_local == drag_id and self.native:
                self.native.released(drag_id)
        elif kind == 'drag-swallow' and self.native:
            if self.context and self.context['id'] == drag_id:
                self.service.drags.handed_off(drag_id, event['peer_id'])
                self.native.send({'op': 'swallow', 'drag': drag_id}) if isinstance(self.native, MacDrag) else self.native.swallow(drag_id)
        elif kind == 'drag-cancel':
            self.cancel_drag(drag_id)
        elif kind == 'drag-receive-error':
            if drag_id in self.incoming:
                self.cancel_drag(drag_id, notify=True)
                self.window.transfer_title.setText('拖放失败')
        elif kind == 'drag-write-error':
            entry = self.incoming.get(drag_id)
            if entry:
                entry['record']['cancel'].set()
            if isinstance(self.native, MacDrag):
                self.native.send({'op': 'written', 'drag': drag_id, 'index': event['index'], 'error': '文件接收失败'})
            self.cleanup(drag_id)
        elif kind == 'drag-settled':
            entry = self.incoming.get(drag_id)
            if entry and not entry.get('saving'):
                if entry['future'].exception() is not None:
                    entry['record']['cancel'].set()
                elif entry['record']['move']:
                    self.window.transfer_title.setText('已复制 · 源文件保留')
                self.cleanup(drag_id)
        elif kind == 'drag-linux-ended':
            QTimer.singleShot(event['delay'], lambda: self.service.emit('drag-settled', drag_id=drag_id))
        elif kind == 'drag-direct-written':
            entry = self.incoming.get(drag_id)
            if entry:
                entry['written'].update(range(len(entry['record']['manifest']['roots'])))
                entry['move_target'] = True
                self.finish_written(drag_id, entry)
        elif kind == 'drag-direct-error':
            if drag_id in self.incoming:
                self.cancel_drag(drag_id, notify=True)
                self.window.transfer_title.setText('落点保存失败 · 源文件已保留')

    def begin(self, drag_id):
        self.service.drags.trace('native-begin', drag_id)
        if not self.native:
            raise TransferError('原生拖放尚未启用')
        record = self.service.drags.incoming.get(drag_id)
        if record is None or drag_id in self.incoming:
            return
        if record.get('released'):
            record['cancel'].set()
            self.incoming[drag_id] = {'record': record}
            self.cleanup(drag_id)
            return
        manifest = record['manifest']
        future = self.service.work_pool.submit(self.service.drags.receive, drag_id)
        self.incoming[drag_id] = {'record': record, 'future': future, 'written': set()}

        def receive_done(result):
            if not result.cancelled() and (error := result.exception()) is not None and not isinstance(error, Cancelled):
                self.service.emit('drag-receive-error', drag_id=drag_id)
        future.add_done_callback(receive_done)
        self.window.transfer.show()
        self.window.transfer_title.setText('正在拖放')
        self.window.transfer_hint.setText('')
        if isinstance(self.native, MacDrag):
            entries = {e['path']: e for e in manifest['entries']}
            items = [{'name': name, 'dir': entries[name]['kind'] == 'directory'} for name in manifest['roots']]
            self.native.send({'op': 'start', 'drag': drag_id, 'items': items,
                              'preview': manifest.get('preview_png') if len(items) == 1 else None})
        else:
            # QDrag enters a nested native loop; defer until the service queue is drained.
            save = (lambda path: self.write_direct(drag_id, path)) if record['move'] else None
            QTimer.singleShot(0, lambda: self.native.incoming(drag_id, manifest, future, save=save)
                              if self.native and drag_id in self.incoming else None)

    def native_event(self, event):
        try:
            if event.get('event') == 'edgeDrag':
                self.source(event)
            elif event.get('event') == 'dragStarted':
                self.service.drags.trace('native-started', event.get('drag'))
            elif event.get('event') == 'dragWrite':
                self.write_promise(event)
            elif event.get('event') == 'dragEnded':
                drag_id = event['drag']
                result = event.get('result')
                reason = event.get('reason')
                if self.resumed_local == drag_id:
                    self.resumed_local = None
                    self.resume_snapshot = None
                    with self.service.lock:
                        outgoing = self.service.drags.outgoing.get(drag_id)
                        if outgoing and outgoing.get('returned'):
                            self.service.drags.outgoing.pop(drag_id)
                    return
                code = 'dropped' if result == 'dropped' else 'failed' if result == 'failed' else 'cancelled'
                if reason in ('busy', 'arming-timeout'):
                    code = reason
                self.service.drags.trace('native-ended', drag_id, code=code)
                entry = self.incoming.get(drag_id)
                if not entry:
                    return
                if event.get('result') != 'dropped':
                    entry['record']['cancel'].set()
                    self.window.transfer_title.setText('拖放已取消')
                    self.clear_context(drag_id)
                    if isinstance(self.native, MacDrag):
                        self.cleanup(drag_id)
                else:
                    self.window.transfer_title.setText('已放下')
                    self.clear_context(drag_id)
                entry['ended'] = True
                if isinstance(self.native, MacDrag):
                    entry['move_target'] = event.get('moveTarget') is True
                    self.finish_written(drag_id, entry)
                else:
                    # On X11 the file manager requests Direct Save after QDrag returns.
                    delay = 7000 if result == 'dropped' and entry['record']['move'] else 0
                    entry['future'].add_done_callback(lambda _: self.service.emit(
                        'drag-linux-ended', drag_id=drag_id, delay=delay))
        except Exception as error:
            self.fail(error)

    def write_direct(self, drag_id, path):
        entry = self.incoming.get(drag_id)
        if not entry or entry.get('saving') or entry['record']['cancel'].is_set():
            raise TransferError('拖放已取消')
        entry['saving'] = True

        def write():
            paths = entry['future'].result()
            destination = Path(path)
            targets = [destination, *(destination.parent / item.name for item in paths[1:])]
            if any(target.exists() or target.is_symlink() for target in targets):
                raise TransferError('目标已有同名文件，源文件已保留')
            for item, target in zip(paths, targets):
                commit_native_destination(item, target, self.service.profile / 'cache', entry['record']['cancel'])

        future = self.service.work_pool.submit(write)
        future.add_done_callback(lambda result: self.service.emit(
            'drag-direct-written' if result.exception() is None else 'drag-direct-error', drag_id=drag_id))
        return future

    def write_promise(self, event):
        drag_id, index = event['drag'], event['index']
        entry = self.incoming.get(drag_id)
        if not entry or type(index) is not int or not 0 <= index < len(entry['record']['manifest']['roots']):
            if isinstance(self.native, MacDrag) and type(index) is int:
                self.native.send({'op': 'written', 'drag': drag_id, 'index': index, 'error': '拖放已取消'})
            return
        # Destination came from our child helper's NSFilePromiseProvider callback.
        destination = event['path']

        def write():
            paths = entry['future'].result()
            return commit_native_destination(paths[index], destination, self.service.profile / 'cache',
                                             entry['record']['cancel'])
        future = self.service.work_pool.submit(write)

        def done(_):
            if self.native:
                self.native.send({'op': 'written', 'drag': drag_id, 'index': index})
            entry['written'].add(index)
            self.finish_written(drag_id, entry)
        # Native promise completion must also report failures to the OS.
        self.watch(future, done)
        future.add_done_callback(lambda f: self.service.emit('drag-write-error', drag_id=drag_id,
            index=index) if f.exception() is not None else None)

    def finish_written(self, drag_id, entry):
        record = entry['record']
        if (self.incoming.get(drag_id) is not entry or not entry.get('ended') or record['cancel'].is_set()
                or len(entry['written']) != len(record['manifest']['roots'])):
            return
        if record['move'] and entry.get('move_target'):
            self.cleanup(drag_id, notify=False)
            future = self.service.io_pool.submit(self.service.drags.confirm_move,
                record['peer'], drag_id, record['manifest']['id'])

            def done(status):
                self.window.transfer_title.setText({'moved': '移动完成', 'retained': '已保存 · 源端有保留文件',
                                                    'unknown': '已保存 · 源端状态待确认'}[status])
            self.watch(future, done)
        else:
            self.window.transfer_title.setText('已复制')
            self.cleanup(drag_id)

    def cleanup(self, drag_id, notify=True):
        entry = self.incoming.pop(drag_id, None)
        self.clear_context(drag_id)
        if entry and notify:
            action = 'cancel' if entry['record']['cancel'].is_set() else 'finish'
            self.watch(self.service.io_pool.submit(self.service.drags.send,
                entry['record']['peer'], action, {'id': drag_id}), lambda _: None)
        with self.service.lock:
            self.service.drags.incoming.pop(drag_id, None)
