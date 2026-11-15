"""Runtime settings, read from environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_POLICY_DIR = Path(__file__).resolve().parents[2] / "policy"


@dataclass(frozen=True)
class Settings:
    policy_dir: Path = field(default_factory=lambda: Path(os.environ.get("AGENTGUARD_POLICY_DIR", REPO_POLICY_DIR)))
    state_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("AGENTGUARD_STATE_DIR", Path.home() / ".agentguard"))
    )
    opa_bin: str = field(default_factory=lambda: os.environ.get("AGENTGUARD_OPA", "opa"))
    kube_context: str | None = field(default_factory=lambda: os.environ.get("AGENTGUARD_KUBE_CONTEXT") or None)
    change_freeze: bool = field(
        default_factory=lambda: os.environ.get("AGENTGUARD_CHANGE_FREEZE", "").lower() in {"1", "true", "yes"}
    )
    approval_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("AGENTGUARD_APPROVAL_TTL", "900")))

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def approvals_file(self) -> Path:
        return self.state_dir / "approvals.json"

    def policy_config(self) -> dict:
        return json.loads((self.policy_dir / "config" / "data.json").read_text())
