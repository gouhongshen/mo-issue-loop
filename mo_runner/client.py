"""Lease one MO environment; model execution stays on the local computer."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid

from mo_runner.pool import Pool
from mo_runner.snapshot import extract, digest_file, source_paths


def bundle(worktree, output):
    root = Path(worktree)
    names = source_paths(root)
    digest = hashlib.sha256()
    with tarfile.open(output, "w:gz", compresslevel=1) as archive:
        for name in names:
            path = root / os.fsdecode(name)
            if not path.exists() and not path.is_symlink():
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError("source directories/symlinks require an explicit snapshot implementation")
            if any(part in {".git", ".env", "auth.json", "credentials.json"} or part.startswith(".env.") for part in Path(os.fsdecode(name)).parts):
                raise ValueError("credential-like file in source snapshot")
            digest.update(name + b"\0" + str(path.stat().st_mode & 0o777).encode() + b"\0")
            with path.open("rb") as reader:
                for chunk in iter(lambda: reader.read(1024 ** 2), b""):
                    digest.update(chunk)
            archive.add(path, arcname=os.fsdecode(name), recursive=False)
    return {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "tree_sha256": digest.hexdigest()}


class Target:
    def __init__(self, state, output, worktree, source, target_branch, build_timeout, test_timeout):
        self.pool, self.output = Pool(state), Path(output)
        self.worktree, self.source, self.target_branch = worktree, source, target_branch
        self.build_timeout, self.test_timeout = build_timeout, test_timeout
        self.id, self.token = uuid.uuid4().hex, None
        self.cancel = threading.Event()
        self.heartbeat = None
        self.heartbeat_error = None
        self.startup_log = self.output / "runner-evidence/logs/startup.jsonl"
        self.info = None

    def request(self, method, action="", **kwargs):
        return self.rpc.call(method, "/tasks/" + self.id + ("/" + action if action else ""), token=self.token, **kwargs)

    def start(self):
        self.output.mkdir(parents=True, exist_ok=True)
        tunnel = self.pool.acquire(self.build_timeout + self.test_timeout + 900)
        self.rpc, self.ports = tunnel.rpc, tunnel.ports[1:]
        with tempfile.TemporaryDirectory(dir=self.output) as directory:
            archive = Path(directory) / "source.tar.gz"
            if bundle(self.worktree, archive) != self.source:
                raise RuntimeError("source changed while preparing runner snapshot")
            lease = self.rpc.call("POST", "/tasks", {"task_id": self.id, "source": self.source,
                "target_branch": self.target_branch, "build_timeout": self.build_timeout,
                "test_timeout": self.test_timeout, "archive_sha256": digest_file(archive)})
            self.token = lease["token"]
            def beat():
                while not self.cancel.wait(30):
                    try:
                        self.request("POST", "heartbeat")
                    except Exception as exc:
                        self.heartbeat_error = str(exc)
                        return
            self.heartbeat = threading.Thread(target=beat, daemon=True)
            self.heartbeat.start()
            self.request("PUT", "source", file=archive)
        self.request("POST", "start")
        deadline = time.monotonic() + self.build_timeout + 360
        while time.monotonic() < deadline:
            self.info = self.request("GET")
            if self.info["phase"] == "FAILED":
                raise RuntimeError(self.info["error"])
            if self.info["phase"] == "READY":
                if self.info["build"]["source"] != self.source:
                    raise RuntimeError("built source identity mismatch")
                self.binary_sha256 = self.info["build"]["binary_sha256"]
                self.executor = {"transport": "github-actions", "run_id": tunnel.manifest["run_id"],
                                 "task_id": self.id, "image": tunnel.manifest["image"]}
                return
            if self.heartbeat_error:
                raise RuntimeError("runner lease renewal failed: " + self.heartbeat_error)
            time.sleep(5)
        raise RuntimeError("remote MO build/start deadline exhausted")

    def start_control(self):
        return {"url": "http://127.0.0.1:{}/tasks/{}".format(self.rpc.port, self.id), "token": self.token}

    def poll(self):
        if self.heartbeat_error:
            raise RuntimeError("runner connection lost: " + self.heartbeat_error)
        info = self.request("GET")
        return None if info["phase"] == "READY" and info["runtime"]["status"] == "READY" else 1

    def close(self):
        errors = []
        try:
            if self.token:
                try:
                    payload = self.request("GET", "artifacts", binary=True)
                    with tempfile.TemporaryDirectory(dir=self.output) as directory:
                        archive = Path(directory) / "evidence.tar.gz"
                        archive.write_bytes(payload)
                        extract(archive, self.output / "runner-evidence", max_bytes=128 * 1024 ** 2)
                except Exception as exc:
                    errors.append("evidence collection: " + str(exc))
                try:
                    self.request("POST", "release")
                except Exception as exc:
                    errors.append("runner release: " + str(exc))
        finally:
            self.cancel.set()
            if self.heartbeat:
                self.heartbeat.join(timeout=190)
            self.pool.close()
        if errors:
            raise RuntimeError("; ".join(errors))
