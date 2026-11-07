"""Compile the exact source snapshot without inheriting host credentials."""
import json
import os
from pathlib import Path
import re
import subprocess
from mo_runner.snapshot import digest_file, tree_digest


def build_environment(source, toolchains=Path("/opt/go")):
    """Use the source's declared toolchain, including experimental API version.

    A newer Go release satisfies the minimum version but can break archsimd.
    Both supported versions are shipped in the image; never download or silently
    substitute a different compiler while executing a candidate build.
    """
    module = (Path(source) / "go.mod").read_text()
    version = re.search(r"(?m)^go (\d+\.\d+\.\d+)\s*(?://[^\n]*)?$", module)
    if not version:
        raise ValueError("source go.mod requires an explicit Go patch version")
    requested = version.group(1)
    toolchain = re.search(r"(?m)^toolchain go(\d+\.\d+\.\d+)\s*(?://[^\n]*)?$", module)
    if toolchain:
        requested = max(requested, toolchain.group(1), key=lambda v: tuple(map(int, v.split('.'))))
    compiler = Path(toolchains) / requested
    executable = compiler / "bin/go"
    if not executable.is_file() or (compiler / "VERSION").read_text().splitlines()[0] != "go" + requested:
        raise RuntimeError("runner image does not contain required Go " + requested + "; rebuild the pinned image")
    environment = dict(os.environ, PATH=str(compiler / "bin") + os.pathsep + os.environ["PATH"],
                       GOROOT=str(compiler), GOTOOLCHAIN="local")
    return environment


def run(root):
    root, source = Path(root), Path(root) / "source"
    request = json.loads((root / "request.json").read_text())
    if tree_digest(source) != request["source"]["tree_sha256"]:
        raise ValueError("source digest differs from requested snapshot")
    environment = build_environment(source)
    toolchain = subprocess.check_output(["go", "version"], env=environment, text=True).strip()
    print("Runner build toolchain: " + toolchain, flush=True)
    for args in (["git", "init", "-q"], ["git", "add", "-f", "."],
                 ["git", "-c", "user.name=Challenger", "-c", "user.email=challenger@localhost",
                  "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "Source snapshot"]):
        subprocess.run(args, cwd=source, check=True)
    subprocess.run(["make", "build", "BRANCH_NAME=" + request["target_branch"],
                    "MO_VERSION=" + request["target_branch"],
                    "LAST_COMMIT_ID=" + request["source"]["head"][:12]], cwd=source, env=environment, check=True)
    (root / "build-result.json").write_text(json.dumps({"source": request["source"],
        "binary_sha256": digest_file(source / "mo-service"),
        "toolchain": toolchain}))


if __name__ == "__main__":
    run("/task")
