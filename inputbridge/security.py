"""Pinned TLS: authenticate the server before sending credentials or filenames."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .files import TransferError

MAX_JSON = 4 * 1024**2


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def identity(profile):
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert_path, key_path = profile / "certificate.pem", profile / "private-key.pem"
    if cert_path.exists() != key_path.exists():
        raise TransferError("设备身份文件不完整，不能自动覆盖已有身份")
    if not cert_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "InputBridge local device")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
                .sign(key, hashes.SHA256()))
        for path, content in [(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                              (cert_path, cert.public_bytes(serialization.Encoding.PEM))]:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context, fingerprint


def validate_device(data):
    if not isinstance(data, dict):
        raise TransferError("设备信息无效")
    if not isinstance(data.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", data["id"]):
        raise TransferError("设备 ID 无效")
    if not isinstance(data.get("fingerprint"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["fingerprint"]):
        raise TransferError("设备证书指纹无效")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
        raise TransferError("设备名称无效")
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 512:
        raise TransferError("连接地址无效")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError:
        raise TransferError("端口无效")
    if (parsed.scheme != "https" or not parsed.hostname or not port or
            parsed.username is not None or parsed.password is not None or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise TransferError("连接地址必须是 HTTPS 主机与端口")
    return {k: data[k] for k in ("id", "name", "fingerprint", "endpoint")}


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, device, timeout=12):
        parsed = urlsplit(device["endpoint"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Replaced by exact certificate pin, before request().
        super().__init__(parsed.hostname, parsed.port, timeout=timeout, context=context)
        self.fingerprint = device["fingerprint"]

    def connect(self):
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, self.fingerprint):
            self.close()
            raise TransferError("设备证书与配对记录不一致，已拒绝连接")


def request_json(device, path, data=None, token=None, local_id=None, timeout=12):
    connection = PinnedConnection(device, timeout)
    headers = {}
    if token:
        headers.update(Authorization="Bearer " + token, **{"X-InputBridge-Device": local_id})
    body = None if data is None else json.dumps(data).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request("GET" if data is None else "POST", path, body, headers)
        response = connection.getresponse()
        raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise TransferError("设备返回的数据过大")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TransferError("设备返回的数据格式不正确")
        if response.status != 200:
            raise TransferError(payload.get("error", "远端拒绝了操作"))
        return payload
    finally:
        connection.close()


def encode_invite(value):
    return "inputbridge://" + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode_invite(value):
    value = value.strip()
    if not value.startswith("inputbridge://") or len(value) > 4096:
        raise TransferError("请粘贴另一台电脑生成的 InputBridge 配对邀请")
    try:
        encoded = value[len("inputbridge://"):]
        data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        validate_device(data["device"])
        if not re.fullmatch(r"[0-9a-f]{64}", data["secret"]):
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError):
        raise TransferError("配对邀请不完整或格式错误")
