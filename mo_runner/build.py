"""Compile the exact source snapshot without inheriting host credentials."""
import json
from pathlib import Path
import subprocess
from mo_runner.snapshot import digest_file, tree_digest


def run(root):
    root, source = Path(root), Path(root) / "source"
    request = json.loads((root / "request.json").read_text())
    if tree_digest(source) != request["source"]["tree_sha256"]:
        raise ValueError("source digest differs from requested snapshot")
    for args in (["git", "init", "-q"], ["git", "add", "-f", "."],
                 ["git", "-c", "user.name=Challenger", "-c", "user.email=challenger@localhost",
                  "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "Source snapshot"]):
        subprocess.run(args, cwd=source, check=True)
    subprocess.run(["make", "build", "BRANCH_NAME=" + request["target_branch"],
                    "MO_VERSION=" + request["target_branch"],
                    "LAST_COMMIT_ID=" + request["source"]["head"][:12]], cwd=source, check=True)
    (root / "build-result.json").write_text(json.dumps({"source": request["source"],
        "binary_sha256": digest_file(source / "mo-service"),
        "toolchain": subprocess.check_output(["go", "version"], text=True).strip()}))


if __name__ == "__main__":
    run("/task")
