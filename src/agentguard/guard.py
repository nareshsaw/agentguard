"""The enforcement point: every tool call goes policy -> (approval) -> dry-run -> execute -> audit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .approvals import ApprovalError, ApprovalStore
from .audit import AuditLog
from .kube import Kube, KubeError
from .policy import OpaPolicy
from .settings import Settings


@dataclass(frozen=True)
class ToolSpec:
    action: str  # "read" or "mutate"
    run: Callable[..., Any]  # (kube, namespace, args, dry_run) -> result
    context: Callable[[Kube, str, dict], dict] | None = None


def _scale_context(kube: Kube, ns: str, args: dict) -> dict:
    return {"current_replicas": kube.deployment(ns, args["name"])["replicas"]}


TOOLS: dict[str, ToolSpec] = {
    "list_pods": ToolSpec("read", lambda k, ns, a, dry: k.list_pods(ns)),
    "get_pod_logs": ToolSpec(
        "read", lambda k, ns, a, dry: k.pod_logs(ns, a["pod"], a.get("tail", 100), a.get("container"),
                                                  a.get("previous", False))
    ),
    "get_events": ToolSpec("read", lambda k, ns, a, dry: k.events(ns, a.get("limit", 25))),
    "describe_deployment": ToolSpec("read", lambda k, ns, a, dry: k.deployment(ns, a["name"])),
    "diagnose_namespace": ToolSpec("read", lambda k, ns, a, dry: k.diagnose(ns)),
    "restart_deployment": ToolSpec("mutate", lambda k, ns, a, dry: k.restart(ns, a["name"], dry)),
    "scale_deployment": ToolSpec(
        "mutate", lambda k, ns, a, dry: k.scale(ns, a["name"], a["replicas"], dry), _scale_context
    ),
    "rollback_deployment": ToolSpec(
        "mutate", lambda k, ns, a, dry: k.rollback(ns, a["name"], a.get("to_revision"), dry)
    ),
    "delete_pod": ToolSpec("mutate", lambda k, ns, a, dry: k.delete_pod(ns, a["pod"], dry)),
}


class Guard:
    def __init__(self, kube: Kube, policy: OpaPolicy, approvals: ApprovalStore, audit: AuditLog,
                 settings: Settings | None = None):
        self.kube, self.policy, self.approvals, self.audit = kube, policy, approvals, audit
        self.settings = settings or Settings()
        self.window = self.settings.policy_config().get("rate_limit", {}).get("window_seconds", 600)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Guard":
        s = settings or Settings()
        return cls(
            Kube(context=s.kube_context),
            OpaPolicy(s.policy_dir, s.opa_bin),
            ApprovalStore(s.approvals_file, s.approval_ttl_seconds),
            AuditLog(s.audit_file),
            s,
        )

    def call(self, tool: str, namespace: str, args: dict | None = None, approval_id: str | None = None) -> dict:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        spec = TOOLS.get(tool)
        base = {"tool": tool, "namespace": namespace, "args": args, "action": spec.action if spec else "unknown"}

        context: dict[str, Any] = {
            "approved": False,
            "recent_mutations": self.audit.recent_mutations(self.window),
            "change_freeze": self.settings.change_freeze,
        }
        try:
            if spec and spec.context:
                context.update(spec.context(self.kube, namespace, args))
        except KubeError as exc:
            return self._finish(base, "failed", reasons=[f"could not read current state: {exc}"])

        if approval_id:
            try:
                self.approvals.check(approval_id, tool, namespace, args)
                context["approved"] = True
            except ApprovalError as exc:
                return self._finish({**base, "approval_id": approval_id}, "denied", reasons=[str(exc)])

        decision = self.policy.evaluate({"tool": tool, "namespace": namespace, "args": args, "context": context})

        if not decision.allow:
            if decision.require_approval:
                req = self.approvals.request(tool, namespace, args, decision.approval_reasons)
                return self._finish(
                    {**base, "approval_id": req["id"]}, "approval_required", reasons=decision.approval_reasons,
                    next_step=(
                        f"A human must approve this change: run `agentguard approve {req['id']}`. "
                        f"Then call {tool} again with the same arguments and approval_id='{req['id']}'."
                    ),
                )
            return self._finish(base, "denied", reasons=decision.deny_reasons)

        try:
            if spec.action == "mutate":
                spec.run(self.kube, namespace, args, True)  # server-side dry run first
                if approval_id:
                    self.approvals.consume(approval_id)
            result = spec.run(self.kube, namespace, args, False)
        except KubeError as exc:
            return self._finish(base, "failed", reasons=[str(exc)])

        extra = {"approval_id": approval_id} if approval_id else {}
        return self._finish({**base, **extra}, "executed", result=result)

    def _finish(self, base: dict, outcome: str, reasons: list[str] | None = None, **extra) -> dict:
        audit_extra = {k: v for k, v in extra.items() if k != "result"}
        self.audit.record(**base, outcome=outcome, reasons=reasons or [], **audit_extra)
        response = {"outcome": outcome, "tool": base["tool"], "namespace": base["namespace"]}
        if reasons:
            response["reasons"] = reasons
        if "approval_id" in base:
            response["approval_id"] = base["approval_id"]
        response.update(extra)
        return response
