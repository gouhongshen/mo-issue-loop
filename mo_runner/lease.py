"""Single-owner admission with a monotonic lifetime and expiring task leases."""
import re
import secrets
import time

RUNNER_LIFETIME_SECONDS = 117 * 60


class Conflict(ValueError):
    pass


class Lease:
    def __init__(self, lifetime=RUNNER_LIFETIME_SECONDS, idle=None, heartbeat=180, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + lifetime
        self.idle_timeout, self.heartbeat_timeout = idle, heartbeat
        self.last_idle = clock()
        self.task = None
        self.draining = False

    def status(self):
        now = self.clock()
        return {"state": "DRAINING" if self.draining else "BUSY" if self.task else "IDLE",
                "remaining_seconds": max(0, int(self.deadline - now)),
                "task_id": self.task["task_id"] if self.task else None}

    def claim(self, task_id, budget):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise ValueError("task_id must be a UUID hex string")
        if type(budget) is not int or not 60 <= budget <= 19800:
            raise ValueError("invalid task budget")
        if self.task:
            raise Conflict("runner already leased; reconcile the existing task")
        if self.draining or self.clock() + budget + 300 >= self.deadline:
            self.draining = True
            raise Conflict("insufficient remaining lifetime")
        self.task = {"task_id": task_id, "token": secrets.token_hex(32),
                     "deadline": self.clock() + budget, "heartbeat": self.clock()}
        return dict(self.task)

    def authorize(self, task_id, token):
        if (not self.task or task_id != self.task["task_id"] or
                not secrets.compare_digest(str(token), self.task["token"])):
            raise PermissionError("invalid or stale task lease")
        if self.clock() >= self.task["deadline"]:
            raise Conflict("task budget exhausted")

    def beat(self, task_id, token):
        self.authorize(task_id, token)
        self.task["heartbeat"] = self.clock()

    def expired(self):
        now = self.clock()
        return bool(self.task and (now >= self.task["deadline"] or
                    now - self.task["heartbeat"] >= self.heartbeat_timeout))

    def release(self):
        self.task = None
        self.last_idle = self.clock()

    def should_exit(self):
        return (self.clock() >= self.deadline or
                (not self.task and (self.draining or
                 (self.idle_timeout is not None and self.clock() - self.last_idle >= self.idle_timeout))))
