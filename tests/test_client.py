from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mo_runner.client import Target


class TargetAdmissionTest(unittest.TestCase):
    def test_snapshot_and_checksum_finish_before_runner_admission(self):
        with tempfile.TemporaryDirectory() as directory, patch("mo_runner.client.Pool") as pool_type:
            source = {"head": "a" * 40, "tree_sha256": "b" * 64}
            prepared = []
            def bundle(worktree, archive):
                Path(archive).write_bytes(b"source")
                prepared.append("snapshot")
                return source
            def digest(archive):
                prepared.append("checksum")
                return "c" * 64
            tunnel = Mock(ports=[1, 2, 3], manifest={"run_id": 7, "image": "pinned"})
            def acquire(request):
                self.assertEqual(prepared, ["snapshot", "checksum"])
                self.assertEqual(request["archive_sha256"], "c" * 64)
                return tunnel, {"token": "reserved"}
            pool_type.return_value.acquire.side_effect = acquire
            tunnel.rpc.call.return_value = {"phase": "READY", "build": {"source": source, "binary_sha256": "d" * 64}}
            target = Target(directory, directory, directory, source, "main", 1800, 3600)
            try:
                with patch("mo_runner.client.bundle", bundle), patch("mo_runner.client.digest_file", digest):
                    target.start()
                self.assertEqual(target.token, "reserved")
                self.assertEqual(target.binary_sha256, "d" * 64)
                self.assertFalse(any(call.args[:2] == ("POST", "/tasks") for call in tunnel.rpc.call.call_args_list))
            finally:
                target.cancel.set()
                if target.heartbeat:
                    target.heartbeat.join(timeout=2)
                target.pool.close()

    def test_changed_snapshot_never_acquires_runner(self):
        with tempfile.TemporaryDirectory() as directory, patch("mo_runner.client.Pool") as pool_type, \
                patch("mo_runner.client.bundle", return_value={"changed": True}):
            target = Target(directory, directory, directory, {"original": True}, "main", 1800, 3600)
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                target.start()
            pool_type.return_value.acquire.assert_not_called()
            target.pool.close()
