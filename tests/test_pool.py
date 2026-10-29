import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mo_runner.pool import Pool, Tunnel


class PoolTest(unittest.TestCase):
    def test_identity_persists_across_controller_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            value = {"instance": "a" * 32, "requested_at": 1, "run_id": 7}
            pool.save(value)
            pool.close()
            pool = Pool(directory)
            self.assertEqual(pool.load(), value)
            pool.close()

    def test_unknown_dispatch_is_reconciled_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": None})
            with patch.object(pool, "gh", return_value={"workflow_runs": []}), \
                    patch.object(pool, "dispatch") as dispatch:
                with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
                    pool.acquire(100)
                dispatch.assert_not_called()
            pool.close()

    def test_failed_instance_retries_only_after_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": 7, "failed_at": 100, "failures": 1})
            with patch("mo_runner.pool.time.time", return_value=200), patch.object(pool, "dispatch") as dispatch, \
                    patch.object(pool, "run_status", return_value={"status": "completed", "head_sha": "a" * 40}), \
                    patch.object(pool, "gh", return_value={"object": {"sha": "a" * 40}}):
                with self.assertRaisesRegex(RuntimeError, "cooling down"):
                    pool.acquire(100)
                dispatch.assert_not_called()
            with patch("mo_runner.pool.time.time", return_value=401), \
                    patch.object(pool, "dispatch", side_effect=RuntimeError("new dispatch")) as dispatch:
                with self.assertRaisesRegex(RuntimeError, "new dispatch"):
                    pool.acquire(100)
                dispatch.assert_called_once()
            pool.close()

    def test_repaired_workflow_can_retry_failed_startup_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": 7, "failed_at": 100, "failures": 3})
            with patch("mo_runner.pool.time.time", return_value=101), \
                    patch.object(pool, "run_status", return_value={"status": "completed", "head_sha": "a" * 40}), \
                    patch.object(pool, "gh", return_value={"object": {"sha": "b" * 40}}), \
                    patch.object(pool, "dispatch", side_effect=RuntimeError("new revision")) as dispatch:
                with self.assertRaisesRegex(RuntimeError, "new revision"):
                    pool.acquire(100)
                dispatch.assert_called_once()
            pool.close()

    def test_tunnel_refuses_public_endpoint_and_unpinned_host(self):
        with tempfile.TemporaryDirectory() as directory:
            for manifest in ({"host": "8.8.8.8", "ssh_port": 2222},
                             {"host": "100.64.0.1", "ssh_port": 22},
                             {"host": "100.64.0.1", "ssh_port": 2222, "host_key": "bad\ninjection"}):
                with self.assertRaises(ValueError):
                    Tunnel(directory, manifest)

    def test_ready_runner_is_reused_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": 7})
            with patch.object(pool, "run_status", return_value={"status": "in_progress", "head_branch": "main", "event": "workflow_dispatch"}), \
                    patch.object(pool, "manifest", return_value={"run_id": 7}), \
                    patch("mo_runner.pool.Tunnel") as tunnel, patch.object(pool, "dispatch") as dispatch:
                tunnel.return_value.start.return_value.call.return_value = {"state": "IDLE", "remaining_seconds": 8000}
                self.assertIs(pool.acquire(3000), tunnel.return_value)
                dispatch.assert_not_called()
            pool.close()
