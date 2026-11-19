#!/usr/bin/env python3
"""Real two-process TLS pairing and bidirectional file transfer on loopback."""
import hashlib
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def node(pipe, profile, name):
    from inputbridge.service import Service
    service = Service(profile, name, poll_interval=.1)
    service.start("127.0.0.1", 0, "127.0.0.1")
    pipe.send(service.create_invite())
    try:
        while True:
            command, value = pipe.recv()
            if command == "stop":
                break
            if command == "pair":
                service.pair(value)
                pipe.send("paired")
            elif command == "copy":
                service.clipboard_changed(value).result(timeout=30)
                pipe.send("offered")
            elif command == "wait":
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    event = service.events.get(timeout=15)
                    if event["kind"] == "error":
                        raise RuntimeError(event["message"])
                    if event["kind"] == "ready":
                        pipe.send(event["paths"])
                        break
                else:
                    raise TimeoutError("No completed transfer")
    except BaseException as error:
        pipe.send({"error": str(error)})
        raise
    finally:
        service.stop()
        pipe.close()


def fingerprint_tree(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def read(pipe):
    if not pipe.poll(35):
        raise TimeoutError("Peer process did not respond")
    value = pipe.recv()
    if isinstance(value, dict) and "error" in value:
        raise RuntimeError(value["error"])
    return value


def main():
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="inputbridge-two-nodes-") as temporary:
        root = Path(temporary)
        source = root / "两台电脑 测试 📁"
        source.mkdir()
        (source / "空目录").mkdir()
        (source / "空文件.txt").write_bytes(b"")
        (source / "binary.bin").write_bytes(bytes(range(256)) * 8192)
        (source / "hello 中文.txt").write_text("文件随行。\n", encoding="utf-8")
        expected = fingerprint_tree(source)
        connections, processes = [], []
        try:
            for name in ["Mac 测试节点", "另一台测试节点"]:
                parent, child = context.Pipe()
                process = context.Process(target=node, args=(child, root / name, name))
                process.start()
                child.close()
                connections.append(parent)
                processes.append(process)
            a, b = connections
            invite_a, _ = read(a), read(b)
            b.send(("pair", invite_a))
            assert read(b) == "paired"
            a.send(("copy", [str(source)]))
            assert read(a) == "offered"
            b.send(("wait", None))
            received_b = read(b)[0]
            assert fingerprint_tree(received_b) == expected
            b.send(("copy", [received_b]))
            assert read(b) == "offered"
            a.send(("wait", None))
            received_a = read(a)[0]
            assert fingerprint_tree(received_a) == expected
            print(json.dumps({"result": "passed", "processes": 2, "directions": ["A→B", "B→A"],
                "transport": "pinned TLS over localhost", "tree_entries": len(expected),
                "checks": ["binary SHA-256", "Unicode and spaces", "empty file", "empty directory"],
                "native_file_manager_paste": "not tested", "cross_os": "not tested"}, indent=2))
        finally:
            for connection in connections:
                try:
                    connection.send(("stop", None))
                except (BrokenPipeError, EOFError, OSError):
                    pass
            for process in processes:
                process.join(12)
                if process.is_alive():
                    process.terminate()
                    process.join(3)
            for connection in connections:
                connection.close()


if __name__ == "__main__":
    main()
