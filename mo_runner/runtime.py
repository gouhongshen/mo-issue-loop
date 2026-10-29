"""Container entrypoint. Fixed controls, never arbitrary remote shell commands."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from mo_runner.topology import configure, SQL_PORTS


def run(root):
    root = Path(root)
    runtime, logs = root / "runtime", root / "logs"
    logs.mkdir(exist_ok=True)
    configure(runtime)
    children = {}
    expected_down = False

    def start(name):
        args = ["/task/source/mo-service", "-launch" if name == "core" else "-cfg",
                str(runtime / ("launch.toml" if name == "core" else "cn1.toml"))]
        with (logs / (name + ".log")).open("ab") as output:
            children[name] = subprocess.Popen(args, cwd=runtime, stdout=output, stderr=subprocess.STDOUT,
                env=dict(os.environ, GOMAXPROCS="3" if name == "core" else "1"), start_new_session=True)

    def state(status, **extra):
        value = {"status": status, "at_epoch": time.time(), **extra}
        target = root / "runtime-state.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(target)

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        start("core")
        deadline = time.monotonic() + 300
        for name, port in zip(("core", "cn1"), SQL_PORTS):
            if name == "cn1":
                start("cn1")
            while True:
                if any(p.poll() is not None for p in children.values()):
                    raise RuntimeError("MO exited during startup")
                probe = subprocess.run(["mysql", "--protocol=TCP", "-h127.0.0.1", "-P" + str(port),
                    "-uroot", "--connect-timeout=2", "-e", "SELECT 1"], env=dict(os.environ, MYSQL_PWD="111"),
                    capture_output=True, timeout=10)
                with (logs / "startup.jsonl").open("a") as output:
                    output.write(json.dumps({"port": port, "at_epoch": time.time(), "returncode": probe.returncode,
                        "stdout": probe.stdout.decode(errors="replace"), "stderr": probe.stderr.decode(errors="replace")}) + "\n")
                if probe.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("SQL startup deadline exhausted")
                time.sleep(2)
        state("READY")
        while True:
            if children["core"].poll() is not None or (not expected_down and children["cn1"].poll() is not None):
                raise RuntimeError("MO process exited unexpectedly")
            command = root / "control-request.json"
            if command.exists():
                request = json.loads(command.read_text())
                command.unlink()
                action = request["action"]
                error = None
                try:
                    if action == "kill":
                        if children["cn1"].poll() is not None:
                            raise ValueError("CN1 is not running")
                        children["cn1"].kill()
                        children["cn1"].wait(timeout=10)
                        expected_down = True
                    elif action == "start":
                        if children["cn1"].poll() is None:
                            raise ValueError("CN1 is already running")
                        start("cn1")
                        expected_down = False
                    elif action != "status":
                        raise ValueError("unsupported control action")
                except ValueError as exc:
                    error = str(exc)
                response = {"id": request["id"], "action": action, "node": 1, "error": error,
                            "pid": children["cn1"].pid, "returncode": children["cn1"].poll(), "at_epoch": time.time()}
                with (logs / "control.jsonl").open("a") as output:
                    output.write(json.dumps(response) + "\n")
                temporary = root / "control-response.tmp"
                temporary.write_text(json.dumps(response))
                temporary.replace(root / "control-response.json")
            time.sleep(0.1)
    except KeyboardInterrupt:
        state("STOPPED")
    except Exception as exc:
        state("FAILED", error=str(exc))
        raise
    finally:
        for child in children.values():
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)


if __name__ == "__main__":
    run("/task")
