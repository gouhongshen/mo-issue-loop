"""Local durable owner for one slot of on-demand GitHub Actions runners."""
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid

from mo_runner.lease import RUNNER_LIFETIME_SECONDS


def command(args, **kwargs):
    return subprocess.check_output(args, timeout=kwargs.pop("timeout", 60), **kwargs)


class RpcError(RuntimeError):
    def __init__(self, status, payload):
        self.status = status
        try:
            self.error = json.loads(payload).get("error")
        except (ValueError, AttributeError):
            self.error = None
        super().__init__("runner HTTP {}: {}".format(status, payload[:1024].decode(errors="replace")))


class Rpc:
    def __init__(self, port, master):
        self.port, self.master = port, master

    def call(self, method, path, value=None, token=None, file=None, binary=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=180)
        headers = {"X-Control-Token": token} if token else {"X-Runner-Token": self.master}
        body = json.dumps(value).encode() if value is not None else b""
        if file:
            headers["Content-Length"] = str(Path(file).stat().st_size)
            body = Path(file).open("rb")
        else:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(128 * 1024 ** 2 + 1)
            if len(payload) > 128 * 1024 ** 2:
                raise RuntimeError("runner response exceeds limit")
            if response.status != 200:
                raise RpcError(response.status, payload)
            return payload if binary else json.loads(payload)
        finally:
            if file:
                body.close()
            connection.close()


class Tunnel:
    def __init__(self, root, manifest):
        self.root, self.manifest, self.process = Path(root), manifest, None
        address = ipaddress.ip_address(manifest["host"])
        if address not in ipaddress.ip_network("100.64.0.0/10") or manifest["ssh_port"] != 2222:
            raise ValueError("invalid runner endpoint")
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?", manifest["host_key"]):
            raise ValueError("invalid SSH host identity")
        known = self.root / "known_hosts"
        known.write_text("[{}]:2222 {}\n".format(address, manifest["host_key"]))
        self.options = ["-F", "/dev/null", "-i", str(self.root / "identity"), "-p", "2222",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + str(known), "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        self.host = "challenger-link@" + str(address)

    def start(self):
        sockets = [socket.socket() for _ in range(3)]
        try:
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            self.ports = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        forward = []
        for local, remote in zip(self.ports, (8765, 26001, 26002)):
            forward += ["-L", "127.0.0.1:{}:127.0.0.1:{}".format(local, remote)]
        with (self.root / "ssh.log").open("ab") as log:
            self.process = subprocess.Popen(["ssh"] + self.options + ["-o", "ExitOnForwardFailure=yes", "-N"] +
                forward + [self.host], stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("runner SSH tunnel failed; inspect ssh.log")
            try:
                with socket.create_connection(("127.0.0.1", self.ports[0]), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("runner SSH tunnel did not start")
        # The forced command can only read this instance's controller token.
        token = command(["ssh"] + self.options + [self.host, "auth"], text=True).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise ValueError("invalid runner authentication response")
        self.rpc = Rpc(self.ports[0], token)
        return self.rpc

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


class Pool:
    def __init__(self, root, repository="gouhongshen/mo-issue-loop", slot=0):
        if repository != "gouhongshen/mo-issue-loop":
            raise ValueError("unexpected runner repository")
        if type(slot) is not int or slot not in (0, 1):
            raise ValueError("invalid runner slot")
        self.slot, self.repository = slot, repository
        self.root = Path(root) if slot == 0 else Path(root) / "slot-1"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = (self.root / "owner.lock").open("a+")
        self.db = sqlite3.connect(str(self.root / "runners.sqlite3"))
        self.db.execute("CREATE TABLE IF NOT EXISTS instance (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)")
        self.tunnel = None

    def save(self, value):
        self.db.execute("INSERT OR REPLACE INTO instance VALUES (1, ?)", (json.dumps(value),))
        self.db.commit()

    def load(self):
        row = self.db.execute("SELECT value FROM instance WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def gh(self, path):
        return json.loads(command(["gh", "api", "repos/" + self.repository + "/" + path], text=True))

    def run_status(self, instance):
        if not instance.get("run_id"):
            runs = self.gh("actions/workflows/challenger.yml/runs?event=workflow_dispatch&per_page=100")["workflow_runs"]
            found = [run for run in runs if run["display_title"] == "challenger-" + instance["instance"]]
            if len(found) > 1:
                raise RuntimeError("duplicate workflow dispatch identity")
            if not found:
                return None
            instance["run_id"] = found[0]["id"]
            self.save(instance)
        return self.gh("actions/runs/" + str(instance["run_id"]))

    def dispatch(self):
        variables = self.gh("actions/variables")["variables"]
        names = {item["name"] for item in variables}
        missing = {"TS_CLIENT_ID", "TS_AUDIENCE", "TS_TAG", "CONTROLLER_IP", "RUNNER_IMAGE"} - names
        if missing:
            raise RuntimeError("runner configuration missing: " + ", ".join(sorted(missing)))
        key = self.root / "identity"
        if not key.exists():
            command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        previous = self.load() or {}
        instance = {"instance": uuid.uuid4().hex, "requested_at": time.time(), "run_id": None,
                    "failures": previous.get("failures", 0)}
        self.save(instance)  # Persist before the external side effect; never blind-redispatch.
        command(["gh", "workflow", "run", "challenger.yml", "--repo", self.repository, "--ref", "main",
                 "-f", "instance=" + instance["instance"], "-f", "slot=" + str(self.slot),
                 "-f", "ssh_public_key=" + key.with_suffix(".pub").read_text().strip()])
        return instance

    def manifest(self, instance):
        artifacts = self.gh("actions/runs/{}/artifacts".format(instance["run_id"]))["artifacts"]
        name = "runner-" + instance["instance"]
        if not any(item["name"] == name and not item["expired"] for item in artifacts):
            return None
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            command(["gh", "run", "download", str(instance["run_id"]), "--repo", self.repository,
                     "--name", name, "--dir", directory])
            value = json.loads((Path(directory) / "runner.json").read_text())
        if value["instance"] != instance["instance"] or value["run_id"] != instance["run_id"]:
            raise ValueError("runner manifest identity mismatch")
        if type(value.get("started_at")) is not int or not 0 < value["started_at"] <= time.time() + 60:
            raise ValueError("invalid runner lifetime anchor")
        return value

    def acquire(self, request):
        budget = request["build_timeout"] + request["test_timeout"] + 900
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        instance = self.load()
        if instance and instance.get("failed_at"):
            delay = min(3600, 300 * 2 ** min(instance.get("failures", 1) - 1, 4))
            if time.time() - instance["failed_at"] < delay:
                failed = self.run_status(instance)
                current = self.gh("git/ref/heads/main")["object"]["sha"]
                if (not failed or failed["status"] != "completed" or
                        failed.get("head_sha") == current):
                    raise RuntimeError("failed runner is cooling down before a new dispatch")
            instance = None
        deadline = time.monotonic() + 1500
        while time.monotonic() < deadline:
            if not instance:
                instance = self.dispatch()
            status = self.run_status(instance)
            if not status:
                if time.time() - instance["requested_at"] > 180:
                    raise RuntimeError("dispatch outcome unknown; refusing duplicate runner creation")
                time.sleep(10)
                continue
            if status["status"] == "completed":
                if status["conclusion"] not in {"success", "cancelled"}:
                    self.save(dict(instance, failed_at=time.time(), failures=instance.get("failures", 0) + 1))
                    raise RuntimeError("runner job failed: " + status["html_url"])
                instance = None
                continue
            if instance.get("draining"):
                # Retirement is authoritative even while GitHub still shows
                # the job running. Do not reconnect to its closing endpoint.
                time.sleep(15)
                continue
            value = self.manifest(instance)
            if not value:
                time.sleep(15)
                continue
            if status["head_branch"] != "main" or status["event"] != "workflow_dispatch":
                raise ValueError("untrusted runner workflow identity")
            tunnel = Tunnel(self.root, value)
            self.tunnel = tunnel
            rpc = tunnel.start()
            try:
                health = rpc.call("GET", "/health")
            except (ConnectionError, OSError, http.client.HTTPException):
                tunnel.close()
                time.sleep(5)
                continue
            remaining = min(health["remaining_seconds"],
                            RUNNER_LIFETIME_SECONDS - max(0, time.time() - value["started_at"]))
            if health["state"] == "IDLE" and remaining > budget + 300:
                try:
                    lease = rpc.call("POST", "/tasks", request)
                except RpcError as exc:
                    # The broker is authoritative. Its lifetime can cross the
                    # admission boundary after /health; this response also
                    # marks it DRAINING. Reconcile its normal retirement.
                    if exc.status != 409 or exc.error != "insufficient remaining lifetime":
                        raise
                    instance = dict(instance, draining=True)
                    self.save(instance)
                else:
                    self.save(dict(instance, failures=0))
                    return tunnel, lease
            elif health["state"] == "IDLE":
                rpc.call("POST", "/drain")
                instance = dict(instance, draining=True)
                self.save(instance)
            elif health["state"] == "DRAINING":
                instance = dict(instance, draining=True)
                self.save(instance)
            tunnel.close()
            time.sleep(15)
        raise RuntimeError("runner acquisition timed out; existing instance retained for reconciliation")

    def close(self):
        if self.tunnel:
            self.tunnel.close()
        self.db.close()
        self.lock.close()
