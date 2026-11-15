"""Append-only JSON Lines audit trail of every tool call and its policy decision."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **event) -> dict:
        now = self.clock()
        entry = {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"), "epoch": now, **event}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def tail(self, limit: int = 20) -> list[dict]:
        return self.entries()[-limit:]

    def recent_mutations(self, window_seconds: int) -> int:
        cutoff = self.clock() - window_seconds
        return sum(
            1 for e in self.entries()
            if e.get("action") == "mutate" and e.get("outcome") == "executed" and e.get("epoch", 0) >= cutoff
        )
