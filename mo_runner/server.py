"""Loopback-only runner broker. Access is carried by a key-pinned SSH tunnel."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import tarfile
import threading
import time

from mo_runner.backend import DockerBackend
from mo_runner.lease import Lease, Conflict
from mo_runner.snapshot import extract, tree_digest


class Broker:
    def __init__(self, root, backend, lifetime=20400, token=None):
        self.root, self.backend = Path(root), backend
        self.root.mkdir(parents=True, exist_ok=True)
        self.lease = Lease(lifetime=lifetime)
        self.lock, self.control_lock = threading.RLock(), threading.Lock()
        self.request, self.task, self.worker = None, None, None
        self.phase, self.error, self.build_info = "IDLE", None, None
        self.token = token or secrets.token_hex(32)
        self.cancel = threading.Event()

    def info(self):
        with self.lock:
            runtime = self.backend.status(self.task) if self.task and self.phase == "READY" else None
            return {**self.lease.status(), "phase": self.phase, "error": self.error,
                    "runtime": runtime, "build": self.build_info}

    def claim(self, value):
        if set(value) != {"task_id", "source", "target_branch", "build_timeout", "test_timeout", "archive_sha256"}:
            raise ValueError("unexpected request fields")
        if (set(value["source"]) != {"head", "tree_sha256"} or
                not re.fullmatch(r"[0-9a-f]{40,64}", value["source"]["head"]) or
                not re.fullmatch(r"[0-9a-f]{64}", value["source"]["tree_sha256"]) or
                not re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"]) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value["target_branch"])):
            raise ValueError("invalid source identity or branch")
        if any(type(value[key]) is not int or not 60 <= value[key] <= 10800 for key in ("build_timeout", "test_timeout")):
            raise ValueError("invalid task timeout")
        if shutil.disk_usage(self.root).free < 6 * 1024 ** 3:
            self.lease.draining = True
            raise Conflict("insufficient disk headroom; runner must be retired")
        with self.lock:
            lease = self.lease.claim(value["task_id"], value["build_timeout"] + value["test_timeout"] + 900)
            self.task = self.root / value["task_id"]
            try:
                self.task.mkdir()
                (self.task / "logs").mkdir()
            except Exception:
                self.lease.draining = True
                self.lease.release()
                raise
            self.request = value
            (self.task / "request.json").write_text(json.dumps(value))
            self.phase, self.error, self.build_info = "UPLOADING", None, None
            self.cancel.clear()
            return {"task_id": value["task_id"], "token": lease["token"]}

    def upload(self, reader, size):
        if not 0 < size <= 512 * 1024 ** 2 or self.phase != "UPLOADING":
            raise Conflict("invalid upload size or phase")
        digest = hashlib.sha256()
        archive = self.task / "source.tar.gz"
        with archive.open("wb") as target:
            remaining = size
            while remaining:
                chunk = reader.read(min(1024 ** 2, remaining))
                if not chunk:
                    raise ValueError("source upload interrupted")
                digest.update(chunk)
                target.write(chunk)
                remaining -= len(chunk)
        if digest.hexdigest() != self.request["archive_sha256"]:
            raise ValueError("archive digest mismatch")
        extract(archive, self.task / "source")
        archive.unlink()
        if tree_digest(self.task / "source") != self.request["source"]["tree_sha256"]:
            raise ValueError("source tree digest mismatch")
        self.phase = "UPLOADED"

    def start(self):
        if self.phase != "UPLOADED":
            raise Conflict("task is not ready to build")
        self.phase = "BUILDING"
        def execute():
            try:
                value = self.backend.build(self.task, self.request)
                if self.cancel.is_set():
                    return
                with self.lock:
                    self.build_info, self.phase = value, "STARTING"
                self.backend.start(self.task, self.request)
                with self.lock:
                    self.phase = "READY"
            except Exception as exc:
                with self.lock:
                    self.phase, self.error = "FAILED", str(exc)
        self.worker = threading.Thread(target=execute, daemon=True)
        self.worker.start()

    def collect(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            remaining = 64 * 1024 ** 2
            for path in sorted((self.task / "logs").rglob("*")):
                if path.is_file() and not path.is_symlink():
                    # Bound diagnostics even when a broken target floods logs.
                    with path.open("rb") as reader:
                        reader.seek(max(0, path.stat().st_size - min(8 * 1024 ** 2, remaining)))
                        content = reader.read(min(8 * 1024 ** 2, remaining))
                    info = tarfile.TarInfo(str(path.relative_to(self.task)))
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                    remaining -= len(content)
                    if not remaining:
                        break
            payload = json.dumps(self.info()).encode()
            info = tarfile.TarInfo("runner-result.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    def release(self):
        # Do not admit a new owner until the old build/start thread has stopped.
        with self.lock:
            task, request, worker = self.task, self.request, self.worker
            self.phase = "CLEANING"
            self.cancel.set()
        if not task:
            return
        self.backend.cleanup(task, request)
        if worker and worker.is_alive():
            worker.join(timeout=45)
            if worker.is_alive():
                self.lease.draining = True
                raise RuntimeError("worker did not stop; runner quarantined")
            self.backend.cleanup(task, request)
        shutil.rmtree(task)
        with self.lock:
            self.task = self.request = self.worker = None
            self.phase, self.error, self.build_info = "IDLE", None, None
            self.lease.release()


def handler(broker):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_request(self):
            try:
                self.connection.settimeout(180)
                if self.path in {"/health", "/tasks", "/drain"}:
                    if not secrets.compare_digest(self.headers.get("X-Runner-Token", ""), broker.token):
                        raise PermissionError("runner controller authentication required")
                    if self.command == "GET" and self.path == "/health":
                        return self.respond(broker.info())
                    if self.command == "POST" and self.path == "/drain":
                        broker.lease.draining = True
                        return self.respond({"ok": True})
                    if self.command == "POST" and self.path == "/tasks":
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 8192:
                            raise ValueError("invalid request size")
                        with broker.control_lock:
                            return self.respond(broker.claim(json.loads(self.rfile.read(length))))
                    raise ValueError("invalid controller request")
                match = re.fullmatch(r"/tasks/([a-f0-9]{32})(?:/(source|start|heartbeat|artifacts|release|cn1/(?:kill|start|status)))?", self.path)
                if not match:
                    raise ValueError("unknown endpoint")
                task_id, action = match.groups()
                with broker.lock:
                    broker.lease.authorize(task_id, self.headers.get("X-Control-Token", ""))
                if self.command == "POST" and action == "heartbeat":
                    with broker.lock:
                        broker.lease.beat(task_id, self.headers.get("X-Control-Token", ""))
                    return self.respond({"ok": True})
                with broker.control_lock:
                    with broker.lock:
                        broker.lease.authorize(task_id, self.headers.get("X-Control-Token", ""))
                    if self.command == "GET" and action is None:
                        return self.respond(broker.info())
                    if self.command == "PUT" and action == "source":
                        broker.upload(self.rfile, int(self.headers.get("Content-Length", "0")))
                    elif self.command == "POST" and action == "start":
                        broker.start()
                    elif self.command == "POST" and action == "release":
                        broker.release()
                    elif self.command == "GET" and action == "artifacts":
                        return self.respond(broker.collect(), binary=True)
                    elif self.command == "POST" and action and action.startswith("cn1/"):
                        if broker.phase != "READY":
                            raise Conflict("MO not ready")
                        return self.respond(broker.backend.control(broker.task, action.rsplit("/", 1)[1]))
                    else:
                        raise ValueError("invalid method")
                    return self.respond({"ok": True})
            except PermissionError as exc:
                self.respond({"error": str(exc)}, 403)
            except Conflict as exc:
                self.respond({"error": str(exc)}, 409)
            except (ValueError, KeyError, TypeError) as exc:
                self.respond({"error": str(exc)}, 400)
            except Exception as exc:
                self.respond({"error": str(exc)}, 500)

        do_GET = do_POST = do_PUT = handle_request

        def respond(self, value, code=200, binary=False):
            payload = value if binary else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/gzip" if binary else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--lifetime", type=int, default=20400)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    broker = Broker(args.root, DockerBackend(args.root, args.image), args.lifetime,
                    Path(args.token_file).read_text().strip())
    server = ThreadingHTTPServer(("127.0.0.1", 8765), handler(broker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while not broker.lease.should_exit():
            if broker.lease.expired():
                with broker.control_lock:
                    broker.release()
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        with broker.control_lock:
            broker.release()


if __name__ == "__main__":
    main()
