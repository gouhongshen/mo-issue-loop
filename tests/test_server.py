import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

from mo_runner.pool import Rpc
from mo_runner.server import Broker, handler
from mo_runner.lease import Conflict


def request(task="a" * 32):
    return {"task_id": task, "source": {"head": "a" * 40, "tree_sha256": "b" * 64},
            "target_branch": "main", "build_timeout": 60, "test_timeout": 60, "archive_sha256": "c" * 64}


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.backend = Mock()
        self.backend.status.return_value = {"status": "READY"}
        self.broker = Broker(self.directory.name, self.backend)

    def tearDown(self):
        self.directory.cleanup()

    def test_rejects_unexpected_credentials_and_shell_fields(self):
        for changes in ({"api_key": "not allowed"}, {"target_branch": "main; touch /tmp/unsafe"},
                        {"build_timeout": True}, {"task_id": "../escape"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.broker.claim(dict(request(), **changes))
        self.assertIsNone(self.broker.lease.task)

    def test_no_disk_means_drain_not_endless_readmission(self):
        with patch("mo_runner.server.shutil.disk_usage", return_value=shutil._ntuple_diskusage(10, 9, 1)):
            with self.assertRaises(Conflict):
                self.broker.claim(request())
        self.assertTrue(self.broker.lease.should_exit())

    def test_release_clears_data_before_next_lease(self):
        old = self.broker.claim(request())
        (self.broker.task / "private-test-data").write_text("old")
        self.broker.release()
        self.backend.cleanup.assert_called_once()
        self.assertFalse((Path(self.directory.name) / old["task_id"]).exists())
        new = self.broker.claim(request("d" * 32))
        self.assertNotEqual(old["token"], new["token"])

    def test_bad_upload_cannot_start(self):
        self.broker.claim(request())
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.broker.upload(io.BytesIO(b"x"), 1)
        with self.assertRaises(Conflict):
            self.broker.start()
        self.backend.build.assert_not_called()

    def test_cleanup_failure_does_not_release_ownership(self):
        self.broker.claim(request())
        self.backend.cleanup.side_effect = RuntimeError("container still present")
        with self.assertRaises(RuntimeError):
            self.broker.release()
        self.assertIsNotNone(self.broker.lease.task)
        with self.assertRaises(Conflict):
            self.broker.claim(request("e" * 32))

    def test_failed_build_never_becomes_ready(self):
        self.broker.claim(request())
        self.broker.phase = "UPLOADED"
        self.backend.build.side_effect = RuntimeError("compiler failed")
        self.broker.start()
        self.broker.worker.join(timeout=3)
        self.assertEqual(self.broker.info()["phase"], "FAILED")
        self.backend.start.assert_not_called()

    def test_http_requires_controller_auth_and_fences_old_tasks(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.broker))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        rpc = Rpc(server.server_port, self.broker.token)
        try:
            with self.assertRaisesRegex(RuntimeError, "403"):
                Rpc(server.server_port, "wrong").call("POST", "/tasks", request())
            old = rpc.call("POST", "/tasks", request())
            rpc.call("POST", "/tasks/" + old["task_id"] + "/release", token=old["token"])
            rpc.call("POST", "/tasks", request("d" * 32))
            with self.assertRaisesRegex(RuntimeError, "403"):
                rpc.call("POST", "/tasks/" + old["task_id"] + "/cn1/kill", token=old["token"])
            self.backend.control.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_artifacts_never_include_source_or_lease_token(self):
        token = self.broker.claim(request())["token"]
        (self.broker.task / "source").mkdir()
        (self.broker.task / "source/secret").write_text("not evidence")
        (self.broker.task / "logs/build.log").write_text("compiler result")
        with tarfile.open(fileobj=io.BytesIO(self.broker.collect()), mode="r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {"logs/build.log", "runner-result.json"})
            self.assertNotIn(token, archive.extractfile("runner-result.json").read().decode())
        self.backend.diagnostics.assert_called_once_with(self.broker.task, self.broker.request)
