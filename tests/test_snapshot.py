import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import subprocess
from mo_runner.client import bundle
from mo_runner.snapshot import extract, tree_digest


class SnapshotTest(unittest.TestCase):
    def test_bundle_transfers_exact_dirty_tree_not_ignored_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "worktree"
            source.mkdir()
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=source, stderr=subprocess.DEVNULL)
            git("init", "-q")
            (source / ".gitignore").write_text("mo-service\n")
            (source / "source.go").write_text("package original\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline")
            (source / "source.go").write_text("package modified\n")
            (source / "test.sql").write_text("SELECT 1;\n")
            (source / "mo-service").write_bytes(b"ignored binary")
            archive = root / "snapshot.tar.gz"
            identity = bundle(source, archive)
            extract(archive, root / "extracted")
            self.assertEqual(identity["head"], git("rev-parse", "HEAD").decode().strip())
            self.assertEqual(identity["tree_sha256"], tree_digest(root / "extracted"))
            self.assertEqual((root / "extracted/source.go").read_text(), "package modified\n")
            self.assertTrue((root / "extracted/test.sql").is_file())
            self.assertFalse((root / "extracted/mo-service").exists())

    def archive(self, root, name="source.go", kind=tarfile.REGTYPE, data=b"package main"):
        archive = root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.size = kind, 0o644, len(data)
            output.addfile(info, io.BytesIO(data))
        return archive

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extract(self.archive(root), root / "target")
            self.assertEqual((root / "target/source.go").read_text(), "package main")
            before = tree_digest(root / "target")
            (root / "target/source.go").write_text("changed")
            self.assertNotEqual(before, tree_digest(root / "target"))

    def test_rejects_traversal_links_credentials_and_expansion(self):
        for name, kind in [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("auth.json", tarfile.REGTYPE), (".env.production", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("device", tarfile.CHRTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ValueError):
                    extract(self.archive(root, name, kind), root / "target")
                self.assertFalse((root / "target").exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                extract(self.archive(root), root / "target", max_bytes=1)
