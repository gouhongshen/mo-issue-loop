import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from mo_runner.snapshot import extract, tree_digest


class SnapshotTest(unittest.TestCase):
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
