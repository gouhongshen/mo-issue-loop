"""Opt-in real MO smoke test: two fresh tasks must reuse the same Actions VM."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mo_runner.client import Target, bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        source = bundle(args.worktree, Path(directory) / "snapshot.tar.gz")
    records = []
    for index in range(args.rounds):
        start = time.monotonic()
        target = Target(args.state, output / str(index), args.worktree, source, "main", 1800, 600)
        try:
            target.start()
            ready = time.monotonic()
            print(json.dumps({"event": "ready", "round": index, "build_start_seconds": ready - start,
                              "executor": target.executor}), flush=True)
            def query(sql, node=0):
                result = subprocess.run(["mysql", "--protocol=TCP", "-h127.0.0.1", "-P" + str(target.ports[node]),
                    "-uroot", "--batch", "--skip-column-names", "--connect-timeout=5", "-e", sql],
                    env=dict(os.environ, MYSQL_PWD="111"), text=True, capture_output=True, timeout=30)
                with (output / str(index) / "queries.jsonl").open("a") as log:
                    log.write(json.dumps({"sql": sql, "node": node, "returncode": result.returncode,
                                          "stdout": result.stdout, "stderr": result.stderr}) + "\n")
                result.check_returncode()
                return result.stdout.strip()
            assert query("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='runner_smoke'") == "0"
            query("CREATE DATABASE runner_smoke; CREATE TABLE runner_smoke.t (id INT PRIMARY KEY); INSERT INTO runner_smoke.t VALUES (1)")
            assert query("SELECT COUNT(*) FROM runner_smoke.t", 1) == "1"
            target.request("POST", "cn1/kill")
            assert query("SELECT COUNT(*) FROM runner_smoke.t", 0) == "1"
            target.request("POST", "cn1/start")
            deadline = time.monotonic() + 60
            while True:
                try:
                    assert query("SELECT COUNT(*) FROM runner_smoke.t", 1) == "1"
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
            assert target.poll() is None
            records.append({"round": index, "executor": target.executor,
                            "build_start_seconds": ready - start, "status": "PASS"})
        finally:
            target.close()
        (output / "summary.json").write_text(json.dumps(records, indent=2))
    assert len({item["executor"]["run_id"] for item in records}) == 1, "VM was not reused"
    print(json.dumps({"status": "PASS", "rounds": records}), flush=True)


if __name__ == "__main__":
    main()
