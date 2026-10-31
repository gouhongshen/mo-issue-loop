import unittest
from mo_runner.lease import Lease, Conflict, RUNNER_LIFETIME_SECONDS


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.lease = Lease(lifetime=1000, idle=100, heartbeat=20, clock=lambda: self.now)
        self.task = "a" * 32

    def test_serial_reuse_and_stale_owner(self):
        first = self.lease.claim(self.task, 500)
        with self.assertRaises(Conflict):
            self.lease.claim("b" * 32, 60)
        self.lease.release()
        second = self.lease.claim("b" * 32, 60)
        self.assertNotEqual(first["token"], second["token"])
        with self.assertRaises(PermissionError):
            self.lease.beat(self.task, first["token"])

    def test_budget_does_not_restart_runner_clock(self):
        self.now += 700
        with self.assertRaises(Conflict):
            self.lease.claim(self.task, 60)
        self.assertTrue(self.lease.should_exit())

    def test_heartbeat_cannot_extend_task_deadline(self):
        value = self.lease.claim(self.task, 60)
        self.now += 59
        self.lease.beat(self.task, value["token"])
        self.now += 1
        self.assertTrue(self.lease.expired())

    def test_orphan_and_idle_exit(self):
        self.lease.claim(self.task, 500)
        self.now += 20
        self.assertTrue(self.lease.expired())
        self.lease.release()
        self.now += 100
        self.assertTrue(self.lease.should_exit())

    def test_default_keeps_idle_runner_until_lifetime_limit(self):
        lease = Lease(clock=lambda: self.now)
        self.now += 1801
        self.assertFalse(lease.should_exit())
        self.now += RUNNER_LIFETIME_SECONDS - 1801
        self.assertTrue(lease.should_exit())

    def test_default_budget_fits_a_fresh_two_hour_runner(self):
        lease = Lease(clock=lambda: self.now)
        self.now += 60
        self.assertEqual(lease.claim(self.task, 1800 + 3600 + 900)["task_id"], self.task)
        lease.release()
        self.now += RUNNER_LIFETIME_SECONDS - 6600 - 60
        with self.assertRaisesRegex(Conflict, "insufficient remaining lifetime"):
            lease.claim("b" * 32, 1800 + 3600 + 900)


if __name__ == "__main__":
    unittest.main()
