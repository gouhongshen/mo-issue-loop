"""Bounded source transfer. Symlinks, devices and credentials never cross it."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile


def source_paths(worktree):
    """Match the loop's content inventory, excluding only empty gitlinks.

    Git creates empty directories for uninitialized submodules. They contain no
    build input; populated submodules remain unsupported and must fail closed.
    """
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=worktree
    ).split(b"\0")
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=worktree).split(b"\0")
    gitlinks = {entry.split(b"\t", 1)[1] for entry in entries if entry.startswith(b"160000 ")}
    for name in gitlinks:
        path = Path(worktree) / os.fsdecode(name)
        if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            raise ValueError("populated submodule requires a separate source snapshot: " + os.fsdecode(name))
    return sorted(set(names) - {b""} - gitlinks)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(archive, destination, max_bytes=2 * 1024 ** 3):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    total, seen = 0, set()
    try:
        with tarfile.open(archive, "r:gz") as source:
            for item in source:
                name = PurePosixPath(item.name)
                if (name.is_absolute() or ".." in name.parts or not name.parts or
                        item.name in seen or not item.isfile() or item.size < 0):
                    raise ValueError("unsafe source archive member")
                if any(part in {".git", ".env", "auth.json", "credentials.json"} or
                       part.startswith(".env.") for part in name.parts):
                    raise ValueError("credentials or git metadata in source archive")
                seen.add(item.name)
                total += item.size
                if total > max_bytes or len(seen) > 100000:
                    raise ValueError("source archive exceeds limits")
                target = destination.joinpath(*name.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                target.chmod(item.mode & 0o777)
    except BaseException:
        shutil.rmtree(destination)
        raise


def tree_digest(directory):
    """Same regular-file identity as issue_loop.challenger.source_identity."""
    root, digest = Path(directory), hashlib.sha256()
    names = sorted(os.fsencode(str(p.relative_to(root))) for p in root.rglob("*") if p.is_file())
    for name in names:
        path = root / os.fsdecode(name)
        if path.is_symlink():
            raise ValueError("source symlink is unsupported")
        digest.update(name + b"\0")
        digest.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
