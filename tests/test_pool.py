import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from mo_runner.pool import Pool, Tunnel, RpcError
from mo_runner.lease import Lease, Conflict


def request():
    return {"task_id": "a" * 32, "build_timeout": 1800, "test_timeout": 3600}


class PoolTest(unittest.TestCase):
    def test_slots_have_independent_durable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Pool(directory, slot=0), Pool(directory, slot=1)
            try:
                first.save({"run_id": 1})
                second.save({"run_id": 2})
                self.assertEqual(first.load(), {"run_id": 1})
                self.assertEqual(second.load(), {"run_id": 2})
                self.assertNotEqual(first.lock.name, second.lock.name)
                self.assertEqual(first.root, Path(directory))
                self.assertEqual(second.root, Path(directory) / "slot-1")
            finally:
                first.close()
                second.close()
            with self.assertRaises(ValueError):
                Pool(directory, slot=2)

    def test_second_slot_is_explicit_in_workflow_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory, slot=1)
            try:
                (pool.root / "identity").write_text("test private key")
                (pool.root / "identity.pub").write_text("ssh-ed25519 test")
                names = ["TS_CLIENT_ID", "TS_AUDIENCE", "TS_TAG", "CONTROLLER_IP", "RUNNER_IMAGE"]
                with patch.object(pool, "gh", return_value={"variables": [{"name": name} for name in names]}), \
                        patch("mo_runner.pool.command") as command:
                    instance = pool.dispatch()
                self.assertIn("slot=1", command.call_args.args[0])
                self.assertEqual(pool.load(), instance)
            finally:
                pool.close()

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
                    pool.acquire(request())
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
                    pool.acquire(request())
                dispatch.assert_not_called()
            with patch("mo_runner.pool.time.time", return_value=401), \
                    patch.object(pool, "dispatch", side_effect=RuntimeError("new dispatch")) as dispatch:
                with self.assertRaisesRegex(RuntimeError, "new dispatch"):
                    pool.acquire(request())
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
                    pool.acquire(request())
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
                    patch.object(pool, "manifest", return_value={"run_id": 7, "started_at": int(time.time())}), \
                    patch("mo_runner.pool.Tunnel") as tunnel, patch.object(pool, "dispatch") as dispatch:
                lease = {"token": "reserved"}
                tunnel.return_value.start.return_value.call.side_effect = [
                    {"state": "IDLE", "remaining_seconds": 8000}, lease]
                self.assertEqual(pool.acquire(request()), (tunnel.return_value, lease))
                dispatch.assert_not_called()
            pool.close()


    def test_lifetime_crossing_retires_runner_before_returning_a_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            old = {"instance": "a" * 32, "requested_at": 1, "run_id": 7}
            new = {"instance": "b" * 32, "requested_at": 2, "run_id": 8}
            pool.save(old)
            now = [0]
            lease = Lease(lifetime=7000, clock=lambda: now[0])
            old_rpc, new_rpc = Mock(), Mock()
            def old_call(method, path, value=None):
                if path == "/health":
                    return lease.status()
                self.assertEqual((method, path), ("POST", "/tasks"))
                now[0] += 800
                try:
                    return lease.claim(value["task_id"], 6300)
                except Conflict as exc:
                    raise RpcError(409, json.dumps({"error": str(exc)}).encode())
            old_rpc.call.side_effect = old_call
            new_rpc.call.side_effect = [{"state": "IDLE", "remaining_seconds": 20000}, {"token": "new lease"}]
            old_tunnel, new_tunnel = Mock(), Mock()
            old_tunnel.start.return_value, new_tunnel.start.return_value = old_rpc, new_rpc
            with patch.object(pool, "run_status", side_effect=[
                    {"status": "in_progress", "head_branch": "main", "event": "workflow_dispatch"},
                    {"status": "in_progress"},  # GitHub has not observed retirement yet.
                    {"status": "completed", "conclusion": "success"},
                    {"status": "in_progress", "head_branch": "main", "event": "workflow_dispatch"}]), \
                    patch.object(pool, "manifest", side_effect=[{"run_id": 7, "started_at": int(time.time())},
                                                               {"run_id": 8, "started_at": int(time.time())}]), \
                    patch.object(pool, "dispatch", return_value=new) as dispatch, \
                    patch("mo_runner.pool.Tunnel", side_effect=[old_tunnel, new_tunnel]), \
                    patch("mo_runner.pool.time.sleep"):
                self.assertEqual(pool.acquire(request()), (new_tunnel, {"token": "new lease"}))
                self.assertTrue(lease.draining)
                self.assertIsNone(lease.task)
                old_tunnel.close.assert_called_once()
                self.assertEqual(old_rpc.call.call_count, 2)
                dispatch.assert_called_once()
                new_rpc.call.assert_called_with("POST", "/tasks", request())
            pool.close()

    def test_other_claim_failures_do_not_dispatch_another_runner(self):
        for status, message in [(409, "runner already leased; reconcile the existing task"),
                                (403, "runner controller authentication required")]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                pool = Pool(directory)
                pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": 7})
                with patch.object(pool, "run_status", return_value={"status": "in_progress", "head_branch": "main", "event": "workflow_dispatch"}), \
                        patch.object(pool, "manifest", return_value={"run_id": 7, "started_at": int(time.time())}), \
                        patch("mo_runner.pool.Tunnel") as tunnel, patch.object(pool, "dispatch") as dispatch:
                    tunnel.return_value.start.return_value.call.side_effect = [
                        {"state": "IDLE", "remaining_seconds": 8000},
                        RpcError(status, json.dumps({"error": message}).encode())]
                    with self.assertRaises(RpcError):
                        pool.acquire(request())
                    dispatch.assert_not_called()
                pool.close()


    def test_retirement_survives_pool_restart_without_reconnecting(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": 7, "draining": True})
            pool.close()
            pool = Pool(directory)
            with patch.object(pool, "run_status", side_effect=[
                    {"status": "in_progress"}, {"status": "completed", "conclusion": "success"}]), \
                    patch.object(pool, "manifest") as manifest, patch("mo_runner.pool.Tunnel") as tunnel, \
                    patch("mo_runner.pool.time.sleep"), \
                    patch.object(pool, "dispatch", side_effect=RuntimeError("replacement requested")):
                with self.assertRaisesRegex(RuntimeError, "replacement requested"):
                    pool.acquire(request())
                manifest.assert_not_called()
                tunnel.assert_not_called()
            pool.close()

    def test_old_long_lived_runner_is_drained_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(directory)
            pool.save({"instance": "a" * 32, "requested_at": 1, "run_id": 7})
            old, new = Mock(), Mock()
            old.start.return_value.call.side_effect = [{"state": "IDLE", "remaining_seconds": 20000}, {}]
            new.start.return_value.call.side_effect = [{"state": "IDLE", "remaining_seconds": 7020}, {"token": "new"}]
            with patch("mo_runner.pool.time.time", return_value=100000), \
                    patch.object(pool, "run_status", side_effect=[
                        {"status": "in_progress", "head_branch": "main", "event": "workflow_dispatch"},
                        {"status": "completed", "conclusion": "success"},
                        {"status": "in_progress", "head_branch": "main", "event": "workflow_dispatch"}]), \
                    patch.object(pool, "manifest", side_effect=[{"run_id": 7, "started_at": 90000},
                                                               {"run_id": 8, "started_at": 100000}]), \
                    patch.object(pool, "dispatch", return_value={"instance": "b" * 32, "run_id": 8}), \
                    patch("mo_runner.pool.Tunnel", side_effect=[old, new]), \
                    patch("mo_runner.pool.time.sleep"):
                self.assertEqual(pool.acquire(request()), (new, {"token": "new"}))
                old.start.return_value.call.assert_any_call("POST", "/drain")
            pool.close()
