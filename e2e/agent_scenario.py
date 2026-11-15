"""End-to-end incident on a real kind cluster, driven through the MCP server over stdio.

A scripted "agent" plays the role an LLM would: it diagnoses a broken rollout, fixes it, then tries
risky actions that the policy must stop. Every step is asserted and written to a Markdown report.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GOOD_IMAGE = "hashicorp/http-echo:1.0"
rows: list[tuple[str, str, str, str]] = []


def payload(result) -> dict:
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc:
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def call(s: ClientSession, step: str, tool: str, args: dict, expect: str | None = None) -> dict:
    r = payload(await s.call_tool(tool, args))
    why = "; ".join(r.get("reasons", [])) or ("approval id " + r["approval_id"] if "approval_id" in r else "")
    rows.append((step, f"`{tool}` {json.dumps({k: v for k, v in args.items() if k != 'namespace'})} "
                       f"ns=`{args.get('namespace', '')}`", r["outcome"], why))
    print(f"[{r['outcome']:>17}] {step}: {tool} {args} {why}", flush=True)
    if expect:
        assert r["outcome"] == expect, f"{step}: expected {expect}, got {r}"
    return r


async def wait_for(check, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await check():
            return
        await anyio.sleep(3)
    raise AssertionError(f"timed out waiting for {what}")


async def scenario():
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentguard", "serve"], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()

        # 1. Diagnose the incident
        async def bad_pod_visible():
            d = payload(await s.call_tool("diagnose_namespace", {"namespace": "staging"}))["result"]
            reasons = {p["reason"] for pod in d["unhealthy_pods"] for p in pod["problems"]}
            return bool(reasons & {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"})

        await wait_for(bad_pod_visible, 120, "the broken pod to show up")
        diag = await call(s, "1. Diagnose staging", "diagnose_namespace", {"namespace": "staging"}, "executed")
        print(json.dumps(diag["result"]["unhealthy_pods"], indent=2))
        dep = await call(s, "2. Inspect the rollout", "describe_deployment",
                         {"namespace": "staging", "name": "web"}, "executed")
        assert dep["result"]["images"] != [GOOD_IMAGE]
        await call(s, "3. Read warning events", "get_events", {"namespace": "staging", "limit": 10}, "executed")

        # 2. Fix it with the smallest safe change
        await call(s, "4. Roll back the bad release", "rollback_deployment",
                   {"namespace": "staging", "name": "web"}, "executed")

        async def healthy():
            d = payload(await s.call_tool("diagnose_namespace", {"namespace": "staging"}))["result"]
            dep = payload(await s.call_tool("describe_deployment", {"namespace": "staging", "name": "web"}))["result"]
            return dep["images"] == [GOOD_IMAGE] and not d["unhealthy_pods"] and dep["ready_replicas"] == 2

        await wait_for(healthy, 180, "staging to recover")
        await call(s, "5. Verify recovery", "diagnose_namespace", {"namespace": "staging"}, "executed")

        # 3. Guardrails: things the agent must NOT be able to do
        pods = payload(await s.call_tool("list_pods", {"namespace": "kube-system"}))["result"]
        coredns = next(p["name"] for p in pods if p["name"].startswith("coredns"))
        await call(s, "6. Delete a system pod", "delete_pod", {"namespace": "kube-system", "pod": coredns}, "denied")
        await call(s, "7. Scale prod 2 -> 50", "scale_deployment",
                   {"namespace": "prod", "name": "web", "replicas": 50}, "denied")
        await call(s, "8. Scale prod to zero", "scale_deployment",
                   {"namespace": "prod", "name": "web", "replicas": 0}, "denied")

        # 4. Human-in-the-loop for production
        req = await call(s, "9. Restart prod", "restart_deployment",
                         {"namespace": "prod", "name": "web"}, "approval_required")
        aid = req["approval_id"]
        await call(s, "10. Retry before a human approves", "restart_deployment",
                   {"namespace": "prod", "name": "web", "approval_id": aid}, "denied")
        out = subprocess.run([sys.executable, "-m", "agentguard", "approve", aid],
                             capture_output=True, text=True, check=True).stdout.strip()
        rows.append(("11. Human approves", f"`agentguard approve {aid}`", "approved", out))
        print(out)
        await call(s, "12. Restart prod with approval", "restart_deployment",
                   {"namespace": "prod", "name": "web", "approval_id": aid}, "executed")
        await call(s, "13. Reuse the same approval", "restart_deployment",
                   {"namespace": "prod", "name": "web", "approval_id": aid}, "denied")

        audit = payload(await s.call_tool("get_audit_log", {"limit": 100}))["entries"]
        assert {"executed", "denied", "approval_required"} <= {e["outcome"] for e in audit}

    restarted = subprocess.run(
        ["kubectl", "get", "deploy", "web", "-n", "prod", "-o",
         "jsonpath={.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}"],
        capture_output=True, text=True, check=True).stdout
    assert restarted, "prod deployment was not restarted"


def report():
    lines = ["## AgentGuard end-to-end run on kind", "",
             "| Step | Tool call | Outcome | Policy reason / detail |", "|---|---|---|---|"]
    icon = {"executed": "✅ executed", "denied": "⛔ denied", "approval_required": "⏸️ approval required",
            "approved": "👤 approved"}
    lines += [f"| {a} | {b} | {icon.get(c, c)} | {d.replace('|', '/')} |" for a, b, c, d in rows]
    text = "\n".join(lines) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)


if __name__ == "__main__":
    try:
        anyio.run(scenario)
    finally:
        report()
