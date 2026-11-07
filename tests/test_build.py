import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mo_runner.build import build_environment


class BuildToolchainTest(unittest.TestCase):
    def test_make_and_nested_go_use_declared_compiler_and_preserve_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            for version in ("1.26.4", "1.27.1"):
                compiler = root / "go" / version
                (compiler / "bin").mkdir(parents=True)
                (compiler / "VERSION").write_text("go" + version + "\ntime metadata\n")
                go = compiler / "bin/go"
                go.write_text("#!/bin/sh\nprintf '%s %s %s %s' '" + version + "' \"$GOROOT\" \"$GOTOOLCHAIN\" \"$GOCACHE\"\n")
                go.chmod(0o755)
            (source / "Makefile").write_text("all:\n\t@go version\n")
            with patch.dict(os.environ, GOCACHE="/cache/custom", GOROOT="/wrong", GOTOOLCHAIN="auto"):
                for version in ("1.26.4", "1.27.1"):
                    with self.subTest(version=version):
                        (source / "go.mod").write_text("module probe\n\ngo " + version + "\n")
                        env = build_environment(source, root / "go")
                        result = subprocess.check_output(["make", "--no-print-directory"], cwd=source, env=env, text=True)
                        self.assertEqual(result, version + " " + str(root / "go" / version) + " local /cache/custom")
                (source / "go.mod").write_text("go 1.26.4\ntoolchain go1.27.1\n")
                self.assertEqual(build_environment(source, root / "go")["GOROOT"], str(root / "go/1.27.1"))

    def test_unavailable_or_mismatched_toolchain_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go.mod").write_text("go 1.26.4\n")
            with self.assertRaisesRegex(RuntimeError, "required Go 1.26.4"):
                build_environment(root, root / "missing")
            (root / "tools/1.26.4/bin").mkdir(parents=True)
            (root / "tools/1.26.4/bin/go").touch()
            (root / "tools/1.26.4/VERSION").write_text("go1.27.1\n")
            with self.assertRaisesRegex(RuntimeError, "required Go 1.26.4"):
                build_environment(root, root / "tools")
            (root / "go.mod").write_text("go 1.26\n")
            with self.assertRaisesRegex(ValueError, "explicit Go patch version"):
                build_environment(root, root / "tools")
