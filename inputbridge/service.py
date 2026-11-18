"""File-only peer service; never captures keyboard, mouse, text or images."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hmac
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import threading
import time
import uuid

from .files import (Cancelled, CHUNK, Offer, TransferError, build_offer,
                    check_cancel, receive_offer, validate_manifest, ID)
from .security import (MAX_JSON, PinnedConnection, atomic_json, decode_invite,
                       encode_invite, identity, request_json, validate_device)


class SendBudget:
    """One smooth upload budget across all peers; leave room for interactive input."""
    def __init__(self, bytes_per_second=4 * 1024**2):
        if bytes_per_second <= 0:
            raise ValueError('Upload budget must be positive')
        self.rate = bytes_per_second
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def wait(self, size, closed):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + size / self.rate
        return not closed.wait(max(0, slot - now))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        self.service = service
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(15)
        return self.service.tls.wrap_socket(connection, server_side=True,
                                            do_handshake_on_connect=False), address

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # No request bodies, credentials or private paths in default logs.


class Handler(BaseHTTPRequestHandler):
    server_version = "InputBridge/0.1"

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authenticated(self):
        ident = self.headers.get("X-InputBridge-Device", "")
        supplied = self.headers.get("Authorization", "")
        with self.server.service.lock:
            peer = self.server.service.peers.get(ident)
            if peer and hmac.compare_digest(supplied, "Bearer " + peer["incoming_token"]):
                return ident
        raise TransferError("设备尚未配对或配对已被移除")

    def do_POST(self):
        if self.path.startswith('/v2/drag/'):
            stage, sender, data = 'auth', None, None
            action = self.path.removeprefix('/v2/drag/')
            try:
                sender = self.authenticated()
                stage = 'body'
                if self.headers.get('Transfer-Encoding'):
                    raise TransferError('无效请求')
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= MAX_JSON:
                    raise TransferError('拖放请求过大')
                data = json.loads(self.rfile.read(size))
                stage = 'session'
                self.server.service.drags.accept(sender, action, data)
                self.server.service.drags.trace('accepted', data.get('id'), action)
                self.reply(200, {'ok': True})
            except (TransferError, ValueError, KeyError, TypeError) as error:
                code = getattr(error, 'code', stage + '-invalid')
                self.server.service.drags.trace('rejected',
                    data.get('id') if isinstance(data, dict) else None, action, code)
                self.reply(400, {'error': '拖放请求失败 [' + code + ']'})
            return
        if self.path == '/v2/input/layout':
            try:
                sender = self.authenticated()
                if self.headers.get('Transfer-Encoding'):
                    raise TransferError('无效请求')
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 8192:
                    raise TransferError('布局请求大小无效')
                self.server.service.accept_input_layout(sender, json.loads(self.rfile.read(size)))
                self.reply(200, {'ok': True})
            except (TransferError, ValueError, KeyError, TypeError):
                self.reply(400, {'error': '未授权或布局无效'})
            return
        if self.path != "/v1/pair":
            self.reply(404, {"error": "未知操作"})
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise TransferError("不接受分块配对请求")
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 8192:
                raise TransferError("配对请求大小无效")
            data = json.loads(self.rfile.read(size))
            result = self.server.service.accept_pair(data)
            self.reply(200, result)
        except (TransferError, ValueError, KeyError, TypeError):
            self.reply(400, {"error": "配对邀请已失效或请求不合法，请重新生成邀请"})

    def do_GET(self):
        try:
            peer_id = self.authenticated()
        except TransferError as error:
            self.reply(401, {"error": str(error)})
            return
        service = self.server.service
        if self.path == "/v1/offer":
            with service.lock:
                offer = service.offer
                ready = (not service.paused and offer is not None and
                         time.monotonic() < offer.expires)
                manifest = offer.manifest if ready else None
            self.reply(200, {"device": service.public_device(), "offer": manifest,
                             "paused": service.paused})
            return
        parts = self.path.split("/")
        drag_request = len(parts) == 6 and parts[1:4] == ['v2', 'drag', 'file']
        if not drag_request and (len(parts) != 5 or parts[1:3] != ["v1", "file"]):
            self.reply(404, {"error": "未知操作"})
            return
        with service.lock:
            offer_id, file_id = (parts[4], parts[5]) if drag_request else (parts[3], parts[4])
            offer = service.drags.source_offer(offer_id, peer_id) if drag_request else service.offer
            valid = not service.paused and offer is not None and offer.manifest["id"] == offer_id
        if not valid:
            self.reply(409, {"error": "复制事件已更改，请重新复制"})
            return
        try:
            handle = offer.open_file(file_id)
        except (TransferError, OSError):
            self.reply(409, {"error": "源文件不可用或已发生变化，请重新复制"})
            return
        size = offer.stamps[file_id][2]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with handle:
            remaining = size
            while remaining:
                with service.lock:
                    current = service.drags.source_offer(offer_id, peer_id) if drag_request else service.offer
                    if service.paused or peer_id not in service.peers or current is not offer:
                        return
                # Small paced writes avoid filling the LAN queue with 256 KiB bursts.
                data = handle.read(min(16 * 1024, remaining))
                if not data:
                    return
                if not service.send_budget.wait(len(data), service.closed):
                    return
                with service.lock:
                    current = service.drags.source_offer(offer_id, peer_id) if drag_request else service.offer
                    if service.paused or peer_id not in service.peers or current is not offer:
                        return
                self.wfile.write(data)
                remaining -= len(data)


class Service:
    def __init__(self, profile, name=None, poll_interval=1.5):
        self.profile = Path(profile)
        self.tls, self.fingerprint = identity(self.profile)
        self.config_path = self.profile / "settings.json"
        self.lock = threading.RLock()
        if self.config_path.exists():
            data = json.loads(self.config_path.read_text())
            self.device_id, self.name, self.peers = data["id"], data["name"], data["peers"]
            self.paused = data.get("paused", False)
            self.auto_receive = data.get("auto_receive", True)
            self.validated_settings()
        else:
            self.device_id = uuid.uuid4().hex
            self.name = (name or socket.gethostname().split(".")[0])[:80]
            self.peers = {}
            self.paused = False
            self.auto_receive = True
        self.endpoint = None
        self.offer = None
        self.revision = 0
        self.receive_generation = 0
        self.events = queue.Queue()
        self.seen, self.online = {}, {}
        self.invite = None
        self.invite_expiry = 0
        self.build_cancel, self.receive_cancel = threading.Event(), threading.Event()
        self.closed = threading.Event()
        self.send_budget = SendBudget()
        self.poll_interval = poll_interval
        self.io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ib-peer")
        self.work_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ib-file")
        self.server = None
        from .drag_transfer import DragTransfers
        self.drags = DragTransfers(self)
        self.save()

    def validated_settings(self):
        import re
        for ident, peer in self.peers.items():
            validate_device(peer)
            if ident != peer["id"]:
                raise TransferError("设备记录不一致")
            for key in ["incoming_token", "outgoing_token"]:
                if not re.fullmatch(r"[0-9a-f]{64}", str(peer.get(key, ""))):
                    raise TransferError("设备授权记录无效")

    def save(self):
        with self.lock:
            atomic_json(self.config_path, {"id": self.device_id, "name": self.name,
                        "peers": self.peers, "paused": self.paused,
                        "auto_receive": self.auto_receive})

    def emit(self, kind, **data):
        self.events.put({"kind": kind, **data})

    def start(self, host="0.0.0.0", port=24820, advertise_host="127.0.0.1", polling=True):
        self.server = Server((host, port), self)
        address = "[" + advertise_host + "]" if ":" in advertise_host else advertise_host
        self.endpoint = "https://%s:%s" % (address, self.server.server_port)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        if polling:
            self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
            self.poll_thread.start()
        return self.endpoint

    def public_device(self):
        return {"id": self.device_id, "name": self.name,
                "fingerprint": self.fingerprint, "endpoint": self.endpoint}

    def create_invite(self):
        with self.lock:
            self.invite = secrets.token_hex(32)
            self.invite_expiry = time.monotonic() + 10 * 60
            return encode_invite({"device": self.public_device(), "secret": self.invite})

    def accept_pair(self, data):
        import re
        device = validate_device(data["device"])
        incoming = data["incoming_token"]
        baseline = data.get("baseline_offer")
        if baseline is not None and not ID.fullmatch(str(baseline)):
            raise TransferError("复制事件 ID 无效")
        if not isinstance(incoming, str) or not re.fullmatch(r"[0-9a-f]{64}", incoming) or device["id"] == self.device_id:
            raise TransferError("设备授权无效")
        with self.lock:
            if (self.invite is None or time.monotonic() > self.invite_expiry or
                    not hmac.compare_digest(str(data["secret"]), self.invite)):
                raise TransferError("配对邀请已失效")
            if device["id"] not in self.peers and len(self.peers) >= 8:
                raise TransferError("最多连接 8 台电脑")
            outgoing = secrets.token_hex(32)
            peer = {**device, "incoming_token": outgoing, "outgoing_token": incoming}
            self.peers[device["id"]] = peer
            self.invite = None
            self.seen[device["id"]] = baseline
            self.save()
        self.emit("peers")
        with self.lock:
            baseline = self.offer.manifest["id"] if self.offer else None
        return {"device": self.public_device(), "incoming_token": outgoing,
                "baseline_offer": baseline}

    def pair(self, invite):
        import re
        data = decode_invite(invite)
        device = data["device"]
        if device["id"] == self.device_id:
            raise TransferError("请在另一台电脑上使用这个邀请")
        with self.lock:
            if device["id"] not in self.peers and len(self.peers) >= 8:
                raise TransferError("最多连接 8 台电脑")
        incoming = secrets.token_hex(32)
        with self.lock:
            baseline = self.offer.manifest["id"] if self.offer else None
        reply = request_json(device, "/v1/pair", {"device": self.public_device(),
                             "secret": data["secret"], "incoming_token": incoming,
                             "baseline_offer": baseline})
        remote = validate_device(reply["device"])
        if (remote["id"] != device["id"] or remote["fingerprint"] != device["fingerprint"] or
                not re.fullmatch(r"[0-9a-f]{64}", str(reply.get("incoming_token", "")))):
            raise TransferError("配对响应身份不一致")
        with self.lock:
            self.peers[device["id"]] = {**device, "incoming_token": incoming,
                                       "outgoing_token": reply["incoming_token"]}
            baseline = reply.get("baseline_offer")
            if baseline is not None and not ID.fullmatch(str(baseline)):
                raise TransferError("复制事件 ID 无效")
            self.seen[device["id"]] = baseline
            self.save()
        self.emit("peers")
        return device["id"]

    def remove_peer(self, ident):
        with self.lock:
            self.drags.revoke(ident)
            self.peers.pop(ident, None)
            self.online.pop(ident, None)
            self.seen.pop(ident, None)
            self.receive_cancel.set()
            self.revision += 1
            self.save()
        self.emit("peers")

    def accept_input_layout(self, sender, value):
        from .engine import validate_layout
        # Initial two-machine setup cannot authorize a third device transitively.
        if not isinstance(value, dict):
            raise TransferError('布局无效')
        if set(value.get('positions', {})) != {self.device_id, sender}:
            raise TransferError('只能配置本次连接的两台电脑')
        with self.lock:
            if sender not in self.peers:
                raise TransferError('设备尚未配对')
            layout = validate_layout(value, self.device_id, self.peers)
            atomic_json(self.profile / 'input-layout.json', layout)
        self.emit('input-layout', layout=layout)

    def configure_input(self, value):
        from .engine import validate_layout
        with self.lock:
            layout = validate_layout(value, self.device_id, self.peers)
            ids = [i for i in layout['positions'] if i != self.device_id]
            if len(ids) != 1:
                raise TransferError('当前请配置两台电脑')
            peer = dict(self.peers[ids[0]])
        request_json(peer, '/v2/input/layout', layout, token=peer['outgoing_token'], local_id=self.device_id)
        with self.lock:
            if peer['id'] not in self.peers:
                raise TransferError('设备配对已被移除')
            atomic_json(self.profile / 'input-layout.json', layout)
        self.emit('input-layout', layout=layout)
        return layout

    def set_paused(self, paused):
        with self.lock:
            self.drags.revoke()
            self.paused = bool(paused)
            self.build_cancel.set()
            self.receive_cancel.set()
            self.offer = None
            self.revision += 1
            self.save()
        self.emit("paused", paused=self.paused)

    def clipboard_changed(self, paths=None):
        with self.lock:
            self.revision += 1
            revision = self.revision
            self.offer = None
            self.build_cancel.set()
            self.receive_cancel.set()
            self.build_cancel = cancel = threading.Event()
        if not paths or self.paused or not self.peers:
            return None
        return self.work_pool.submit(self.prepare, list(paths), revision, cancel)

    def prepare(self, paths, revision, cancel):
        self.emit("preparing")
        try:
            offer = build_offer(paths, cancel)
            with self.lock:
                if revision != self.revision or self.paused:
                    return
                self.offer = offer
            self.emit("offered", count=len(paths), total=offer.manifest["total_bytes"])
        except Cancelled:
            pass
        except (TransferError, OSError) as error:
            self.emit("error", message=str(error))

    def poll_loop(self):
        pending, retry_at, failures = {}, {}, {}
        while not self.closed.wait(self.poll_interval):
            with self.lock:
                peers = list(self.peers)
            now = time.monotonic()
            for ident in peers:
                future = pending.get(ident)
                if future is not None:
                    if not future.done():
                        continue
                    success = future.result()
                    failures[ident] = 0 if success else min(failures.get(ident, 0) + 1, 5)
                    retry_at[ident] = now + (0 if success else 2 ** failures[ident])
                    pending.pop(ident)
                if now >= retry_at.get(ident, 0):
                    pending[ident] = self.io_pool.submit(self.poll_peer, ident)

    def poll_peer(self, ident):
        with self.lock:
            if ident not in self.peers:
                return False
            peer = dict(self.peers[ident])
        try:
            reply = request_json(peer, "/v1/offer", token=peer["outgoing_token"],
                                 local_id=self.device_id, timeout=4)
            device = validate_device(reply["device"])
            if device["id"] != ident or device["fingerprint"] != peer["fingerprint"]:
                raise TransferError("对端设备身份不一致")
            manifest = reply.get("offer")
            if manifest is not None:
                validate_manifest(manifest)
            with self.lock:
                if ident not in self.peers:
                    return False
                previous = self.seen.get(ident, "first-poll")
                offer_id = manifest["id"] if manifest else None
                self.seen[ident] = offer_id
                changed = not self.online.get(ident, False)
                self.online[ident] = True
                receive = (manifest is not None and previous != offer_id and
                           previous != "first-poll" and not self.paused and self.auto_receive)
            if changed:
                self.emit("peers")
            if receive:
                self.schedule_receive(ident, manifest)
            return True
        except (TransferError, OSError, HTTPException, ValueError, KeyError, TypeError):
            with self.lock:
                changed = self.online.get(ident, False)
                self.online[ident] = False
            if changed:
                self.emit("peers")
            return False

    def schedule_receive(self, ident, manifest):
        with self.lock:
            self.receive_cancel.set()
            self.receive_cancel = cancel = threading.Event()
            revision = self.revision
            self.receive_generation += 1
            generation = self.receive_generation
            peer = dict(self.peers[ident])
        return self.work_pool.submit(self.receive, peer, manifest, revision, cancel, generation)

    def receive(self, peer, manifest, revision, cancel, generation):
        self.emit("receiving", peer=peer["name"], total=manifest["total_bytes"], generation=generation)
        last_progress = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if now - last_progress[0] > 0.15 or done == total:
                last_progress[0] = now
                self.emit("progress", done=done, total=total, generation=generation)

        @contextmanager
        def fetch(entry):
            check_cancel(cancel)
            connection = PinnedConnection(peer, timeout=30)
            try:
                connection.request("GET", "/v1/file/%s/%s" % (manifest["id"], entry["id"]),
                    headers={"Authorization": "Bearer " + peer["outgoing_token"],
                             "X-InputBridge-Device": self.device_id})
                response = connection.getresponse()
                if response.status != 200 or response.getheader("Content-Length") != str(entry["size"]):
                    raise TransferError("源文件已更改或设备拒绝传输，请重新复制")
                yield response
            finally:
                connection.close()
        try:
            paths = receive_offer(manifest, fetch, self.profile / "cache", cancel, progress)
            with self.lock:
                if (self.paused or revision != self.revision or peer["id"] not in self.peers or
                        generation != self.receive_generation or cancel.is_set()):
                    return
            self.emit("ready", paths=[str(p) for p in paths], revision=revision,
                      generation=generation, peer=peer["name"], offer_id=manifest["id"])
        except Cancelled:
            self.emit("cancelled", generation=generation)
        except (TransferError, OSError, HTTPException, ValueError) as error:
            self.emit("error", message=str(error), generation=generation)

    def stop(self):
        with self.lock:
            self.drags.revoke()
        self.closed.set()
        self.build_cancel.set()
        self.receive_cancel.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.io_pool.shutdown(wait=True, cancel_futures=True)
        self.work_pool.shutdown(wait=True, cancel_futures=True)
        for handler in list(self.drags.log.handlers):
            handler.close()
            self.drags.log.removeHandler(handler)
