"""Thin, typed wrapper around kubectl. Returns compact summaries an AI agent can reason about."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Callable

Runner = Callable[[list[str]], str]


class KubeError(Exception):
    pass


def _kubectl_runner(context: str | None) -> Runner:
    def run(args: list[str]) -> str:
        cmd = ["kubectl", *(["--context", context] if context else []), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise KubeError(proc.stderr.strip() or f"kubectl exited with {proc.returncode}")
        return proc.stdout

    return run


def _dry(dry_run: bool) -> list[str]:
    return ["--dry-run=server"] if dry_run else []


class Kube:
    def __init__(self, runner: Runner | None = None, context: str | None = None):
        self.run = runner or _kubectl_runner(context)

    def _json(self, args: list[str]) -> dict:
        return json.loads(self.run([*args, "-o", "json"]))

    # ------------------------------------------------------------------ reads
    def list_pods(self, namespace: str) -> list[dict]:
        pods = self._json(["get", "pods", "-n", namespace])["items"]
        return [_pod_summary(p) for p in pods]

    def pod_logs(self, namespace: str, pod: str, tail: int = 100, container: str | None = None,
                 previous: bool = False) -> str:
        args = ["logs", pod, "-n", namespace, f"--tail={tail}"]
        if container:
            args += ["-c", container]
        if previous:
            args.append("--previous")
        return self.run(args)

    def events(self, namespace: str, limit: int = 25) -> list[dict]:
        items = self._json(["get", "events", "-n", namespace])["items"]
        items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        return [
            {
                "type": e.get("type"),
                "reason": e.get("reason"),
                "object": f"{e['involvedObject'].get('kind')}/{e['involvedObject'].get('name')}",
                "message": (e.get("message") or "").strip(),
                "count": e.get("count", 1),
                "last_seen": e.get("lastTimestamp") or e.get("eventTime"),
            }
            for e in items[-limit:]
        ]

    def deployment(self, namespace: str, name: str) -> dict:
        d = self._json(["get", "deployment", name, "-n", namespace])
        return _deployment_summary(d)

    def deployments(self, namespace: str) -> list[dict]:
        return [_deployment_summary(d) for d in self._json(["get", "deployments", "-n", namespace])["items"]]

    def diagnose(self, namespace: str) -> dict:
        pods = self.list_pods(namespace)
        unhealthy = [p for p in pods if not p["healthy"]]
        degraded = [d for d in self.deployments(namespace) if d["ready_replicas"] < d["replicas"]]
        warnings = [e for e in self.events(namespace, limit=50) if e["type"] == "Warning"][-10:]
        return {
            "namespace": namespace,
            "summary": (
                "healthy" if not unhealthy and not degraded
                else f"{len(unhealthy)} unhealthy pod(s), {len(degraded)} degraded deployment(s)"
            ),
            "unhealthy_pods": unhealthy,
            "degraded_deployments": degraded,
            "recent_warning_events": warnings,
        }

    # ---------------------------------------------------------------- changes
    def restart(self, namespace: str, name: str, dry_run: bool = False) -> str:
        # Same patch `kubectl rollout restart` sends, but `rollout restart` has no --dry-run flag.
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
        return self.run(
            ["patch", f"deployment/{name}", "-n", namespace, "--type=merge", "-p", json.dumps(patch), *_dry(dry_run)]
        ).strip()

    def scale(self, namespace: str, name: str, replicas: int, dry_run: bool = False) -> str:
        return self.run(
            ["scale", f"deployment/{name}", f"--replicas={replicas}", "-n", namespace, *_dry(dry_run)]
        ).strip()

    def rollback(self, namespace: str, name: str, to_revision: int | None = None, dry_run: bool = False) -> str:
        args = ["rollout", "undo", f"deployment/{name}", "-n", namespace, *_dry(dry_run)]
        if to_revision:
            args.append(f"--to-revision={to_revision}")
        return self.run(args).strip()

    def delete_pod(self, namespace: str, pod: str, dry_run: bool = False) -> str:
        return self.run(["delete", "pod", pod, "-n", namespace, "--wait=false", *_dry(dry_run)]).strip()


def _pod_summary(p: dict) -> dict:
    statuses = p.get("status", {}).get("containerStatuses", []) or []
    ready = sum(1 for c in statuses if c.get("ready"))
    total = len(p.get("spec", {}).get("containers", []))
    problems = []
    for c in statuses:
        waiting = (c.get("state") or {}).get("waiting")
        if waiting and waiting.get("reason") not in (None, "ContainerCreating", "PodInitializing"):
            problems.append({"container": c["name"], "reason": waiting.get("reason"),
                             "message": waiting.get("message", ""), "image": c.get("image")})
        last = (c.get("lastState") or {}).get("terminated")
        if last and last.get("reason") not in (None, "Completed"):
            problems.append({"container": c["name"], "reason": f"last exit: {last.get('reason')}",
                             "message": f"exit code {last.get('exitCode')}", "image": c.get("image")})
    for cond in p.get("status", {}).get("conditions", []) or []:
        if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
            problems.append({"container": None, "reason": cond.get("reason"), "message": cond.get("message", "")})
    phase = p.get("status", {}).get("phase")
    return {
        "name": p["metadata"]["name"],
        "phase": phase,
        "ready": f"{ready}/{total}",
        "restarts": sum(c.get("restartCount", 0) for c in statuses),
        "node": p.get("spec", {}).get("nodeName"),
        "owner": next((o.get("name") for o in p["metadata"].get("ownerReferences", [])), None),
        "healthy": phase in ("Running", "Succeeded") and ready == total and not problems,
        "problems": problems,
    }


def _deployment_summary(d: dict) -> dict:
    spec, status = d.get("spec", {}), d.get("status", {})
    return {
        "name": d["metadata"]["name"],
        "replicas": spec.get("replicas", 0),
        "ready_replicas": status.get("readyReplicas", 0),
        "updated_replicas": status.get("updatedReplicas", 0),
        "revision": int(d["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", 0)),
        "images": [c["image"] for c in spec.get("template", {}).get("spec", {}).get("containers", [])],
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"), "message": c.get("message")}
            for c in status.get("conditions", [])
        ],
    }
