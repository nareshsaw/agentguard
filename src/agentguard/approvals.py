"""Human-in-the-loop approvals. The agent can request one; only a human can grant it.

Approvals are bound to the exact action (tool + namespace + arguments), expire, and can be used once.
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path


def fingerprint(tool: str, namespace: str, args: dict) -> str:
    payload = json.dumps({"tool": tool, "namespace": namespace, "args": args}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class ApprovalError(Exception):
    pass


class ApprovalStore:
    def __init__(self, path: Path, ttl_seconds: int = 900, clock=time.time):
        self.path = Path(path)
        self.ttl = ttl_seconds
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        lock = self.path.with_suffix(".lock")
        with lock.open("w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            yield data
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True))
            fcntl.flock(lf, fcntl.LOCK_UN)

    def request(self, tool: str, namespace: str, args: dict, reasons: list[str]) -> dict:
        now = self.clock()
        req = {
            "id": "apr-" + secrets.token_hex(4),
            "tool": tool,
            "namespace": namespace,
            "args": args,
            "fingerprint": fingerprint(tool, namespace, args),
            "reasons": reasons,
            "status": "pending",
            "created": now,
            "expires": now + self.ttl,
        }
        with self._locked() as data:
            data[req["id"]] = req
        return req

    def list(self, status: str | None = None) -> list[dict]:
        with self._locked() as data:
            items = list(data.values())
        return [i for i in items if status is None or i["status"] == status]

    def decide(self, approval_id: str, approve: bool, by: str | None = None) -> dict:
        with self._locked() as data:
            req = data.get(approval_id)
            if req is None:
                raise ApprovalError(f"unknown approval id {approval_id}")
            if req["status"] != "pending":
                raise ApprovalError(f"approval {approval_id} is already {req['status']}")
            if self.clock() > req["expires"]:
                req["status"] = "expired"
                raise ApprovalError(f"approval {approval_id} has expired")
            req["status"] = "approved" if approve else "denied"
            req["decided_by"] = by or getpass.getuser()
            req["decided_at"] = self.clock()
            return dict(req)

    def check(self, approval_id: str, tool: str, namespace: str, args: dict) -> None:
        """Raise ApprovalError unless approval_id is a valid, unused approval for exactly this action."""
        with self._locked() as data:
            req = data.get(approval_id)
            if req is None:
                raise ApprovalError(f"unknown approval id {approval_id}")
            if req["fingerprint"] != fingerprint(tool, namespace, args):
                raise ApprovalError(f"approval {approval_id} was granted for a different action")
            if req["status"] == "pending":
                raise ApprovalError(f"approval {approval_id} is still waiting for a human")
            if req["status"] == "used":
                raise ApprovalError(f"approval {approval_id} has already been used (approvals are single-use)")
            if req["status"] != "approved":
                raise ApprovalError(f"approval {approval_id} was {req['status']} by a human")
            if self.clock() > req["expires"]:
                req["status"] = "expired"
                raise ApprovalError(f"approval {approval_id} has expired")

    def consume(self, approval_id: str) -> None:
        with self._locked() as data:
            data[approval_id]["status"] = "used"
            data[approval_id]["used_at"] = self.clock()
