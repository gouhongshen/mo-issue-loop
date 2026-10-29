"""Owned Docker containers with bounded resources and per-task scratch."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


class DockerBackend:
    def __init__(self, root, image):
        self.root, self.image = Path(root), image
        self.cache = self.root / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def base(self, task, name):
        return ["docker", "run", "--name", name, "--init", "--cpus=4", "--memory=12g", "--memory-swap=12g",
                "--pids-limit=2048", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user",
                str(os.getuid()) + ":" + str(os.getgid()), "--env", "HOME=/tmp",
                "--mount", "type=bind,source=" + str(task) + ",target=/task",
                "--mount", "type=bind,source=" + str(self.cache) + ",target=/cache"]

    def name(self, task_id, role):
        return "mo-challenge-" + task_id + "-" + role

    def build(self, task, request):
        name = self.name(request["task_id"], "build")
        with (task / "logs/build.log").open("wb") as log:
            command = self.base(task, name) + [self.image, "python3", "-m", "mo_runner.build"]
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=request["build_timeout"])
            except subprocess.TimeoutExpired:
                self.remove(name)
                process.wait(timeout=30)
                raise
        self.remove(name)
        if code:
            raise RuntimeError("build failed with exit {}".format(code))
        return json.loads((task / "build-result.json").read_text())

    def start(self, task, request):
        name = self.name(request["task_id"], "runtime")
        command = self.base(task, name) + ["--publish", "127.0.0.1:26001:26001", "--publish", "127.0.0.1:26002:26002",
                   "--detach", self.image, "python3", "-m", "mo_runner.runtime"]
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        deadline = time.monotonic() + 330
        while time.monotonic() < deadline:
            state = self.status(task)
            if state["status"] == "READY":
                return
            if state["status"] == "FAILED":
                raise RuntimeError(state.get("error", "MO startup failed"))
            running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", name], text=True, timeout=15)
            if running.strip() != "true":
                raise RuntimeError("MO container exited during startup")
            time.sleep(2)
        raise RuntimeError("MO startup timed out")

    def status(self, task):
        state = task / "runtime-state.json"
        return json.loads(state.read_text()) if state.exists() else {"status": "STARTING"}

    def control(self, task, action):
        if action not in {"status", "kill", "start"}:
            raise ValueError("unsupported CN1 action")
        request_id = uuid.uuid4().hex
        temporary = task / "control-request.tmp"
        temporary.write_text(json.dumps({"action": action, "id": request_id}))
        temporary.replace(task / "control-request.json")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = task / "control-response.json"
            if response.exists():
                value = json.loads(response.read_text())
                if value.get("id") == request_id:
                    if value.get("error"):
                        raise ValueError(value["error"])
                    return value
            time.sleep(0.1)
        raise RuntimeError("CN control outcome unknown; inspect status before retrying")

    def remove(self, name):
        known = subprocess.check_output(["docker", "ps", "-a", "--format", "{{.Names}}"], text=True, timeout=30).splitlines()
        if name in known:
            subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True, timeout=60)

    def cleanup(self, task, request):
        for role in ("runtime", "build"):
            name = self.name(request["task_id"], role)
            if role == "runtime":
                result = subprocess.run(["docker", "logs", name], capture_output=True, timeout=30)
                (task / "logs/container.log").write_bytes(result.stdout + result.stderr)
            self.remove(name)
