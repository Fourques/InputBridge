"""Bounded file offers and private, transactional receive staging."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
import unicodedata
import uuid

CHUNK = 256 * 1024
MAX_ENTRIES = 10000
MAX_BYTES = 64 * 1024**3
OFFER_TTL = 30 * 60
ID = re.compile(r"^[0-9a-f]{32}$")
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class TransferError(Exception):
    pass


class Cancelled(TransferError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("传输已取消")


def valid_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise TransferError("文件路径不合法")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) or
           any(ord(c) < 32 or c in '\\:*?"<>|\x7f' for c in p) or RESERVED.match(p)
           for p in parts):
        raise TransferError("文件名不能安全地在三系统之间使用")
    if PurePosixPath(value).is_absolute():
        raise TransferError("不接受绝对路径")
    return parts


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@dataclass
class Offer:
    manifest: dict
    sources: dict
    stamps: dict
    expires: float

    def open_file(self, entry_id):
        if time.monotonic() > self.expires or entry_id not in self.sources:
            raise TransferError("复制的文件已过期，请重新复制")
        fd = os.open(self.sources[entry_id], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        handle = os.fdopen(fd, "rb")
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stamp(info) != self.stamps[entry_id]:
            handle.close()
            raise TransferError("源文件已改变，请重新复制")
        return handle


def build_offer(paths, cancel=None):
    entries, sources, stamps, roots, names = [], {}, {}, [], set()
    total = 0

    def visit(path, relative):
        nonlocal total
        check_cancel(cancel)
        valid_path(relative)
        canonical = unicodedata.normalize("NFD", relative).casefold()
        if canonical in names:
            raise TransferError("文件名在大小写或 Unicode 规范化后冲突")
        names.add(canonical)
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
            raise TransferError("初版不传输符号链接，请选择实际文件")
        if len(entries) >= MAX_ENTRIES:
            raise TransferError("单次复制最多包含 10000 个文件和目录")
        item = {"id": uuid.uuid4().hex, "path": relative}
        if stat.S_ISDIR(info.st_mode):
            item["kind"] = "directory"
            entries.append(item)
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                visit(child, relative + "/" + child.name)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > MAX_BYTES:
                raise TransferError("初版单次复制上限为 64 GiB")
            digest = hashlib.sha256()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if stamp(before) != stamp(info):
                    raise TransferError("源文件在准备期间发生变化")
                while True:
                    check_cancel(cancel)
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                if stamp(os.fstat(handle.fileno())) != stamp(before):
                    raise TransferError("源文件在准备期间发生变化")
            item.update(kind="file", size=info.st_size, sha256=digest.hexdigest())
            entries.append(item)
            sources[item["id"]] = str(path)
            stamps[item["id"]] = stamp(info)
        else:
            raise TransferError("只能复制普通文件和目录")

    for value in paths:
        path = Path(os.path.abspath(value))
        if not path.name:
            raise TransferError("不能复制整个磁盘根目录")
        roots.append(path.name)
        visit(path, path.name)
    if not roots:
        raise TransferError("没有选中文件")
    manifest = {"version": 1, "id": uuid.uuid4().hex, "roots": roots,
                "entries": entries, "total_bytes": total}
    return Offer(manifest, sources, stamps, time.monotonic() + OFFER_TTL)


def validate_manifest(data):
    if (not isinstance(data, dict) or data.get("version") != 1 or
            not isinstance(data.get("id"), str) or not ID.fullmatch(data["id"])):
        raise TransferError("不支持的文件清单")
    entries, roots = data.get("entries"), data.get("roots")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise TransferError("文件清单超出限制")
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ENTRIES:
        raise TransferError("没有有效的顶层文件")
    if any(not isinstance(r, str) or len(valid_path(r)) != 1 for r in roots):
        raise TransferError("顶层文件不合法")
    if len(set(roots)) != len(roots):
        raise TransferError("顶层文件重复")
    names, ids, known, total = set(), set(), {}, 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise TransferError("文件清单格式错误")
        relative = entry.get("path")
        parts = valid_path(relative)
        key = unicodedata.normalize("NFD", relative).casefold()
        ident = entry.get("id")
        if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in ids or key in names:
            raise TransferError("文件清单包含重复或无效对象")
        ids.add(ident)
        names.add(key)
        if parts[0] not in roots:
            raise TransferError("文件不属于本次复制范围")
        if len(parts) > 1 and known.get("/".join(parts[:-1])) != "directory":
            raise TransferError("缺少目录或目录层次不合法")
        kind = entry.get("kind")
        if kind == "file":
            size = entry.get("size")
            if type(size) is not int or size < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                raise TransferError("文件大小或校验值不合法")
            total += size
        elif kind != "directory":
            raise TransferError("文件类型不支持")
        known[relative] = kind
    if total > MAX_BYTES or type(data.get("total_bytes")) is not int or total != data["total_bytes"]:
        raise TransferError("传输总量不合法")
    if any(r not in known for r in roots):
        raise TransferError("缺少顶层文件")
    return data


def receive_offer(manifest, fetch_file, cache, cancel=None, progress=None):
    manifest = validate_manifest(manifest)
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(cache).free < manifest["total_bytes"] + 16 * 1024**2:
        raise TransferError("接收端磁盘空间不足")
    ident = uuid.uuid4().hex
    staging, completed = cache / (ident + ".part"), cache / ident
    staging.mkdir(mode=0o700)
    received = 0
    try:
        for entry in manifest["entries"]:
            check_cancel(cancel)
            target = staging.joinpath(*valid_path(entry["path"]))
            if entry["kind"] == "directory":
                target.mkdir(mode=0o700)
                continue
            digest, written = hashlib.sha256(), 0
            with fetch_file(entry) as response, target.open("xb") as handle:
                os.chmod(target, 0o600)
                while True:
                    check_cancel(cancel)
                    chunk = response.read(min(CHUNK, entry["size"] - written + 1))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > entry["size"]:
                        raise TransferError("接收的数据超过声明大小")
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, manifest["total_bytes"])
                if written != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise TransferError("文件不完整或校验不一致，未发布到剪贴板")
                handle.flush()
                os.fsync(handle.fileno())
        check_cancel(cancel)
        staging.rename(completed)
        return [completed / name for name in manifest["roots"]]
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
