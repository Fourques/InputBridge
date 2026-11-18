"""Connect real edge drags, native input focus, and authenticated file sessions."""
from pathlib import Path
import sys
import time

from PySide6.QtCore import QObject, QProcess, QTimer

from .drag_native import LinuxDrag, MacDrag
from .drag_transfer import commit_native_destination
from .engine import screen_name
from .files import TransferError


class DragBridge(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.service, self.engine = window, window.service, window.engine
        self.native = None
        self.peer_id = None
        self.context = None
        self.incoming = {}
        self.callbacks = []
        self.last_arm = 0
        self.engine.process.started.connect(self.start)
        self.engine.process.finished.connect(self.stop)
        self.engine.native_event.connect(self.input_event)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(25)

    def watch(self, future, callback):
        self.callbacks.append((future, callback))

    def tick(self):
        for future, callback in list(self.callbacks):
            if future.done():
                self.callbacks.remove((future, callback))
                try:
                    callback(future.result())
                except Exception as error:
                    self.fail(error)
        context = self.context
        if (context and context.get('armed') and not context.get('released') and
                self.engine.layout()['controller_id'] == self.service.device_id and
                time.monotonic() - self.last_arm > .5):
            try:
                self.engine.arm_file_drag()
                self.last_arm = time.monotonic()
            except (OSError, TransferError):
                pass

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
        for entry in self.incoming.values():
            entry['record']['cancel'].set()
        self.incoming.clear()

    def source(self, event):
        if not self.native or not self.peer_id:
            return
        drag_id = event.get('drag')
        self.service.drags.trace('native-edge', drag_id)
        paths = event.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            return
        self.context = {'id': drag_id, 'source': self.service.device_id,
                        'target': self.peer_id, 'armed': False, 'released': False}
        self.window.transfer.show()
        self.window.transfer_title.setText('准备拖放')
        self.window.transfer_hint.setText('')
        future = self.service.work_pool.submit(self.service.drags.prepare, self.peer_id, paths, drag_id)

        def ready(_):
            if not self.context or self.context['id'] != drag_id or self.context.get('released'):
                self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'cancel', {'id': drag_id})
                return
            self.context['armed'] = True
            if self.engine.layout()['controller_id'] == self.service.device_id:
                self.engine.arm_file_drag()
            else:
                future = self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'arm', {'id': drag_id})
                self.watch(future, lambda _: None)
        self.watch(future, ready)

    def input_event(self, event):
        context = self.context
        if not context:
            return
        drag_id = context['id']
        if (event['event'] == 'focus' and context.get('crossed') and
                event.get('from') == screen_name(context['target']) and
                event.get('to') == screen_name(context['source'])):
            self.watch(self.service.io_pool.submit(self.service.drags.send,
                self.peer_id, 'cancel', {'id': drag_id}), lambda _: None)
            self.engine.arm_file_drag(False)
            self.context = None
            return
        if event['event'] == 'focus' and context.get('armed'):
            if event.get('from') != screen_name(context['source']) or event.get('to') != screen_name(context['target']):
                return
            context['crossed'] = True
            self.service.drags.trace('input-crossed', drag_id)
            if context['source'] == self.service.device_id:
                self.native.send({'op': 'swallow', 'drag': drag_id}) if isinstance(self.native, MacDrag) else self.native.swallow()
                future = self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'begin', {'id': drag_id})
                self.watch(future, lambda _: None)
            else:
                record = self.service.drags.incoming[drag_id]
                if not record['begun']:
                    record['begun'] = True
                    self.service.emit('drag-begin', drag_id=drag_id, peer_id=self.peer_id)
                future = self.service.io_pool.submit(self.service.drags.send, self.peer_id, 'swallow', {'id': drag_id})
                self.watch(future, lambda _: None)
        elif event['event'] == 'button-up' and event.get('button') == 1:
            context['released'] = True
            self.engine.arm_file_drag(False)
            if context.get('crossed') and context['target'] != self.service.device_id:
                self.watch(self.service.io_pool.submit(self.service.drags.send,
                    self.peer_id, 'button-up', {'id': drag_id}), lambda _: None)
            if context.get('crossed') and context['target'] == self.service.device_id and isinstance(self.native, MacDrag):
                self.native.send({'op': 'buttonUp', 'drag': drag_id})

    def service_event(self, event):
        kind, drag_id = event['kind'], event['drag_id']
        if kind == 'drag-arm':
            if self.engine.layout()['controller_id'] != self.service.device_id:
                return
            self.context = {'id': drag_id, 'source': event['peer_id'],
                            'target': self.service.device_id, 'armed': True, 'released': False}
            self.engine.arm_file_drag()
        elif kind == 'drag-begin':
            self.begin(drag_id)
        elif kind == 'drag-button-up' and self.native:
            if isinstance(self.native, MacDrag):
                self.native.send({'op': 'buttonUp', 'drag': drag_id})
            else:
                self.native.released(drag_id)
        elif kind == 'drag-finish':
            if self.context and self.context['id'] == drag_id:
                self.engine.arm_file_drag(False)
                self.context = None
                self.window.transfer_title.setText('拖放完成')
        elif kind == 'drag-swallow' and self.native:
            self.native.send({'op': 'swallow', 'drag': drag_id}) if isinstance(self.native, MacDrag) else self.native.swallow()
        elif kind == 'drag-cancel' and self.native:
            self.native.send({'op': 'cancel', 'drag': drag_id}) if isinstance(self.native, MacDrag) else self.native.cancel()
            if self.context and self.context['id'] == drag_id:
                self.engine.arm_file_drag(False)
                self.context = None
        elif kind == 'drag-write-error':
            entry = self.incoming.get(drag_id)
            if entry:
                entry['record']['cancel'].set()
            if isinstance(self.native, MacDrag):
                self.native.send({'op': 'written', 'drag': drag_id, 'index': event['index'], 'error': '文件接收失败'})
            self.cleanup(drag_id)
        elif kind == 'drag-settled':
            entry = self.incoming.get(drag_id)
            if entry and entry['future'].exception() is not None:
                entry['record']['cancel'].set()
            self.cleanup(drag_id)

    def begin(self, drag_id):
        self.service.drags.trace('native-begin', drag_id)
        if not self.native:
            raise TransferError('原生拖放尚未启用')
        record = self.service.drags.incoming[drag_id]
        if record.get('released'):
            record['cancel'].set()
            self.incoming[drag_id] = {'record': record}
            self.cleanup(drag_id)
            return
        manifest = record['manifest']
        future = self.service.work_pool.submit(self.service.drags.receive, drag_id)
        self.incoming[drag_id] = {'record': record, 'future': future, 'written': set()}
        self.window.transfer.show()
        self.window.transfer_title.setText('正在拖放')
        self.window.transfer_hint.setText('')
        if isinstance(self.native, MacDrag):
            entries = {e['path']: e for e in manifest['entries']}
            items = [{'name': name, 'dir': entries[name]['kind'] == 'directory'} for name in manifest['roots']]
            self.native.send({'op': 'start', 'drag': drag_id, 'items': items})
        else:
            # QDrag enters a nested native loop; defer until the service queue is drained.
            QTimer.singleShot(0, lambda: self.native.incoming(drag_id, manifest, future))

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
                self.service.drags.trace('native-ended', drag_id,
                    code='dropped' if event.get('result') == 'dropped' else 'cancelled')
                entry = self.incoming.get(drag_id)
                if not entry:
                    return
                if event.get('result') != 'dropped':
                    entry['record']['cancel'].set()
                    self.window.transfer_title.setText('拖放已取消')
                else:
                    self.window.transfer_title.setText('已放下')
                entry['ended'] = True
                if not isinstance(self.native, MacDrag):
                    entry['future'].add_done_callback(lambda _: self.service.emit(
                        'drag-settled', drag_id=drag_id))
        except Exception as error:
            self.fail(error)

    def write_promise(self, event):
        drag_id, index = event['drag'], event['index']
        entry = self.incoming.get(drag_id)
        if not entry or type(index) is not int or not 0 <= index < len(entry['record']['manifest']['roots']):
            return
        # Destination came from our child helper's NSFilePromiseProvider callback.
        destination = event['path']

        def write():
            paths = entry['future'].result()
            return commit_native_destination(paths[index], destination, self.service.profile / 'cache')
        future = self.service.work_pool.submit(write)

        def done(_):
            if self.native:
                self.native.send({'op': 'written', 'drag': drag_id, 'index': index})
            entry['written'].add(index)
            if len(entry['written']) == len(entry['record']['manifest']['roots']):
                self.window.transfer_title.setText('拖放完成')
                self.cleanup(drag_id)
        # Native promise completion must also report failures to the OS.
        self.callbacks.append((future, done))
        future.add_done_callback(lambda f: self.service.emit('drag-write-error', drag_id=drag_id,
            index=index) if f.exception() is not None else None)

    def cleanup(self, drag_id):
        entry = self.incoming.pop(drag_id, None)
        if entry:
            action = 'cancel' if entry['record']['cancel'].is_set() else 'finish'
            self.watch(self.service.io_pool.submit(self.service.drags.send,
                entry['record']['peer'], action, {'id': drag_id}), lambda _: None)
        with self.service.lock:
            self.service.drags.incoming.pop(drag_id, None)
