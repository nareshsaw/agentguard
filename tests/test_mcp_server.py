"""Talks to the real MCP server over stdio, exactly like Claude Desktop or VS Code would."""
import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result):
    assert not result.is_error, result.content
    sc = getattr(result, "structured_content", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def _session_flow():
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentguard", "serve"], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert {"diagnose_namespace", "rollback_deployment", "delete_pod", "get_audit_log"} <= set(tools)
            assert tools["delete_pod"].annotations.destructive_hint is True
            assert tools["list_pods"].annotations.read_only_hint is True

            diag = payload(await session.call_tool("diagnose_namespace", {"namespace": "staging"}))
            assert diag["result"]["unhealthy_pods"][0]["problems"][0]["reason"] == "ImagePullBackOff"

            denied = payload(await session.call_tool("delete_pod", {"namespace": "kube-system", "pod": "coredns-0"}))
            assert denied["outcome"] == "denied"

            fixed = payload(await session.call_tool("rollback_deployment", {"namespace": "staging", "name": "web"}))
            assert fixed["outcome"] == "executed"

            audit = payload(await session.call_tool("get_audit_log", {"limit": 10}))
            assert [e["outcome"] for e in audit["entries"]] == ["executed", "denied", "executed"]


def test_mcp_stdio_session(env, monkeypatch):
    src = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(src), os.environ.get("PYTHONPATH", "")]))
    anyio.run(_session_flow)
