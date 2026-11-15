from agentguard.approvals import ApprovalStore
from agentguard.audit import AuditLog
from agentguard.policy import OpaPolicy

from conftest import kube_state, kubectl_calls


def test_diagnose_finds_the_broken_rollout(guard):
    r = guard.call("diagnose_namespace", "staging")
    assert r["outcome"] == "executed"
    pod = r["result"]["unhealthy_pods"][0]
    assert pod["problems"][0]["reason"] == "ImagePullBackOff"
    assert r["result"]["degraded_deployments"][0]["name"] == "web"


def test_rollback_in_staging_is_dry_run_then_executed(guard, env):
    r = guard.call("rollback_deployment", "staging", {"name": "web"})
    assert r["outcome"] == "executed"
    undo_calls = [c for c in kubectl_calls(env) if c[:2] == ["rollout", "undo"]]
    assert "--dry-run=server" in undo_calls[0] and "--dry-run=server" not in undo_calls[1]
    assert kube_state(env)["staging"]["web"]["image"] == "web:1.0"


def test_protected_namespace_is_denied_and_never_touched(guard, env):
    r = guard.call("delete_pod", "kube-system", {"pod": "coredns-0"})
    assert r["outcome"] == "denied"
    assert "protected" in r["reasons"][0]
    assert not any(c[0] == "delete" for c in kubectl_calls(env))


def test_unknown_tool_is_denied(guard):
    assert guard.call("exec_into_pod", "staging", {"pod": "web-0"})["outcome"] == "denied"


def test_scale_limits(guard, env):
    assert guard.call("scale_deployment", "prod", {"name": "web", "replicas": 50})["outcome"] == "denied"
    assert guard.call("scale_deployment", "prod", {"name": "web", "replicas": 0})["outcome"] == "denied"
    assert guard.call("scale_deployment", "staging", {"name": "web", "replicas": 4})["outcome"] == "executed"
    assert kube_state(env)["staging"]["web"]["replicas"] == 4


def test_prod_change_needs_human_approval_end_to_end(guard, env):
    first = guard.call("restart_deployment", "prod", {"name": "web"})
    assert first["outcome"] == "approval_required"
    aid = first["approval_id"]

    # the agent cannot use the id before a human approves it
    early = guard.call("restart_deployment", "prod", {"name": "web"}, approval_id=aid)
    assert early["outcome"] == "denied" and "waiting for a human" in early["reasons"][0]

    guard.approvals.decide(aid, approve=True, by="naresh")

    # the approval is bound to this exact action
    other = guard.call("restart_deployment", "prod", {"name": "api"}, approval_id=aid)
    assert other["outcome"] == "denied" and "different action" in other["reasons"][0]

    ok = guard.call("restart_deployment", "prod", {"name": "web"}, approval_id=aid)
    assert ok["outcome"] == "executed"
    assert kube_state(env)["prod"]["web"]["revision"] == 2

    # and it is single-use
    again = guard.call("restart_deployment", "prod", {"name": "web"}, approval_id=aid)
    assert again["outcome"] == "denied" and "used" in again["reasons"][0]


def test_denied_approval(guard):
    aid = guard.call("restart_deployment", "prod", {"name": "web"})["approval_id"]
    guard.approvals.decide(aid, approve=False, by="naresh")
    r = guard.call("restart_deployment", "prod", {"name": "web"}, approval_id=aid)
    assert r["outcome"] == "denied" and "denied" in r["reasons"][0]


def test_rate_limit(guard):
    for _ in range(5):
        assert guard.call("restart_deployment", "staging", {"name": "web"})["outcome"] == "executed"
    r = guard.call("restart_deployment", "staging", {"name": "web"})
    assert r["outcome"] == "denied" and "rate limit" in r["reasons"][0]
    assert guard.call("list_pods", "staging")["outcome"] == "executed"  # reads still work


def test_change_freeze(env, monkeypatch):
    monkeypatch.setenv("AGENTGUARD_CHANGE_FREEZE", "true")
    from agentguard.guard import Guard
    r = Guard.from_settings().call("restart_deployment", "staging", {"name": "web"})
    assert r["outcome"] == "denied" and "change freeze" in r["reasons"][0]


def test_every_call_is_audited(guard):
    guard.call("list_pods", "staging")
    guard.call("delete_pod", "kube-system", {"pod": "x"})
    guard.call("restart_deployment", "prod", {"name": "web"})
    outcomes = [e["outcome"] for e in guard.audit.entries()]
    assert outcomes == ["executed", "denied", "approval_required"]


def test_policy_engine_failure_fails_closed(env, tmp_path):
    d = OpaPolicy(tmp_path / "does-not-exist").evaluate({"tool": "list_pods"})
    assert d.allow is False and "failing closed" in d.deny_reasons[0]


def test_missing_deployment_is_reported_not_crashed(guard):
    r = guard.call("scale_deployment", "staging", {"name": "nope", "replicas": 1})
    assert r["outcome"] == "failed" and "not found" in r["reasons"][0]


def test_expired_approval(env):
    clock = [1000.0]
    store = ApprovalStore(env / "a.json", ttl_seconds=60, clock=lambda: clock[0])
    req = store.request("restart_deployment", "prod", {"name": "web"}, [])
    store.decide(req["id"], approve=True, by="naresh")
    clock[0] += 61
    import pytest
    from agentguard.approvals import ApprovalError
    with pytest.raises(ApprovalError, match="expired"):
        store.check(req["id"], "restart_deployment", "prod", {"name": "web"})


def test_audit_window(tmp_path):
    clock = [0.0]
    log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: clock[0])
    log.record(tool="x", action="mutate", outcome="executed")
    clock[0] = 700
    log.record(tool="x", action="mutate", outcome="executed")
    log.record(tool="x", action="mutate", outcome="denied")
    assert log.recent_mutations(600) == 1


def test_restart_uses_a_dry_run_capable_patch(guard, env):
    assert guard.call("restart_deployment", "staging", {"name": "web"})["outcome"] == "executed"
    patches = [c for c in kubectl_calls(env) if c[0] == "patch"]
    assert len(patches) == 2 and "--dry-run=server" in patches[0] and "--dry-run=server" not in patches[1]
    assert "kubectl.kubernetes.io/restartedAt" in patches[1][patches[1].index("-p") + 1]
