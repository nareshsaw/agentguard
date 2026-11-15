"""MCP server exposing guarded Kubernetes tools to AI assistants (Claude, VS Code Copilot, ...)."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .guard import Guard

INSTRUCTIONS = """\
You are operating a Kubernetes cluster through AgentGuard. Every call is checked against
OPA/Rego policy and written to an audit log.
- Start with diagnose_namespace, then use logs, events and describe_deployment to find the root cause.
- Prefer the smallest safe fix (rollback a bad rollout, restart a stuck deployment).
- If a call returns outcome=denied, do not try to work around it; explain the reasons to the user.
- If a call returns outcome=approval_required, tell the user the approval id and wait for them to approve,
  then call the same tool again with approval_id.
"""

mcp = MCPServer("agentguard", instructions=INSTRUCTIONS)
_guard: Guard | None = None

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def guard() -> Guard:
    global _guard
    if _guard is None:
        _guard = Guard.from_settings()
    return _guard


@mcp.tool(annotations=READ)
def diagnose_namespace(namespace: str) -> dict[str, Any]:
    """Health check of a namespace: unhealthy pods with reasons, degraded deployments, recent warning events."""
    return guard().call("diagnose_namespace", namespace)


@mcp.tool(annotations=READ)
def list_pods(namespace: str) -> dict[str, Any]:
    """List pods in a namespace with phase, readiness, restarts and any problems."""
    return guard().call("list_pods", namespace)


@mcp.tool(annotations=READ)
def get_pod_logs(namespace: str, pod: str, tail: int = 100, container: str | None = None,
                 previous: bool = False) -> dict[str, Any]:
    """Return the last `tail` log lines of a pod. Set previous=true for the crashed container's logs."""
    return guard().call("get_pod_logs", namespace,
                        {"pod": pod, "tail": tail, "container": container, "previous": previous})


@mcp.tool(annotations=READ)
def get_events(namespace: str, limit: int = 25) -> dict[str, Any]:
    """Return recent Kubernetes events in a namespace (oldest first)."""
    return guard().call("get_events", namespace, {"limit": limit})


@mcp.tool(annotations=READ)
def describe_deployment(namespace: str, name: str) -> dict[str, Any]:
    """Show a deployment's replicas, readiness, current revision, images and conditions."""
    return guard().call("describe_deployment", namespace, {"name": name})


@mcp.tool(annotations=CHANGE)
def restart_deployment(namespace: str, name: str, approval_id: str | None = None) -> dict[str, Any]:
    """Rolling-restart a deployment. Policy-checked; may require human approval."""
    return guard().call("restart_deployment", namespace, {"name": name}, approval_id)


@mcp.tool(annotations=CHANGE)
def scale_deployment(namespace: str, name: str, replicas: int, approval_id: str | None = None) -> dict[str, Any]:
    """Scale a deployment. Policy limits the step size and maximum; may require human approval."""
    return guard().call("scale_deployment", namespace, {"name": name, "replicas": replicas}, approval_id)


@mcp.tool(annotations=CHANGE)
def rollback_deployment(namespace: str, name: str, to_revision: int | None = None,
                        approval_id: str | None = None) -> dict[str, Any]:
    """Roll a deployment back to its previous revision (or to_revision). Policy-checked."""
    return guard().call("rollback_deployment", namespace, {"name": name, "to_revision": to_revision}, approval_id)


@mcp.tool(annotations=CHANGE)
def delete_pod(namespace: str, pod: str, approval_id: str | None = None) -> dict[str, Any]:
    """Delete a single pod so its controller recreates it. Policy-checked."""
    return guard().call("delete_pod", namespace, {"pod": pod}, approval_id)


@mcp.tool(annotations=READ)
def get_audit_log(limit: int = 20) -> dict[str, Any]:
    """Show the most recent AgentGuard decisions (what was allowed, denied or sent for approval, and why)."""
    entries = guard().audit.tail(limit)
    return {"outcome": "executed", "entries": [{k: v for k, v in e.items() if k != "epoch"} for e in entries]}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
