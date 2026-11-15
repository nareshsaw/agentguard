import json
import os
import shutil
from pathlib import Path

import pytest

from agentguard.approvals import ApprovalStore
from agentguard.audit import AuditLog
from agentguard.guard import Guard
from agentguard.kube import Kube
from agentguard.policy import OpaPolicy
from agentguard.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
FAKE_BIN = ROOT / "tests" / "bin"

INITIAL_STATE = {
    "staging": {"web": {"replicas": 2, "image": "web:2.0-broken", "previous_image": "web:1.0",
                        "revision": 2, "broken": True}},
    "prod": {"web": {"replicas": 2, "image": "web:1.0", "revision": 1}},
    "kube-system": {"coredns": {"replicas": 2, "image": "coredns:1.11", "revision": 1}},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not shutil.which("opa"):
        pytest.skip("opa CLI not installed")
    state = tmp_path / "kube.json"
    state.write_text(json.dumps(INITIAL_STATE))
    monkeypatch.setenv("FAKE_KUBE_STATE", str(state))
    monkeypatch.setenv("FAKE_KUBE_LOG", str(tmp_path / "kubectl.log"))
    monkeypatch.setenv("PATH", f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTGUARD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENTGUARD_POLICY_DIR", str(ROOT / "policy"))
    return tmp_path


@pytest.fixture
def guard(env):
    s = Settings()
    return Guard(Kube(), OpaPolicy(s.policy_dir), ApprovalStore(s.approvals_file), AuditLog(s.audit_file), s)


def kube_state(env):
    return json.loads((env / "kube.json").read_text())


def kubectl_calls(env):
    path = env / "kubectl.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
