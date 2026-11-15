"""Evaluates every tool call against the OPA/Rego policy. Fails closed."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Decision:
    allow: bool
    require_approval: bool = False
    deny_reasons: list[str] = field(default_factory=list)
    approval_reasons: list[str] = field(default_factory=list)


class OpaPolicy:
    """Runs `opa eval` against the policy bundle in policy_dir."""

    def __init__(self, policy_dir: Path, opa_bin: str = "opa", timeout: float = 10.0):
        self.policy_dir = Path(policy_dir)
        self.opa_bin = opa_bin
        self.timeout = timeout

    def evaluate(self, policy_input: dict) -> Decision:
        cmd = [
            self.opa_bin, "eval", "--format", "json", "--data", str(self.policy_dir),
            "--stdin-input", "data.agentguard.decision",
        ]
        try:
            proc = subprocess.run(
                cmd, input=json.dumps(policy_input), capture_output=True, text=True, timeout=self.timeout, check=False
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"opa exited with {proc.returncode}")
            value = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
        except Exception as exc:  # fail closed: no decision means no action
            return Decision(allow=False, deny_reasons=[f"policy engine error (failing closed): {exc}"])
        return Decision(
            allow=bool(value.get("allow")),
            require_approval=bool(value.get("require_approval")),
            deny_reasons=list(value.get("deny_reasons", [])),
            approval_reasons=list(value.get("approval_reasons", [])),
        )
