"""AgentGuard CLI for the human side: approve or deny agent requests and read the audit log.

    agentguard serve              start the MCP server on stdio
    agentguard pending            list approval requests waiting for a human
    agentguard approve <id>       approve a request
    agentguard deny <id>          deny a request
    agentguard audit [-n 20]      show the latest audit entries
"""
from __future__ import annotations

import argparse
import json
import sys

from .approvals import ApprovalError, ApprovalStore
from .audit import AuditLog
from .settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentguard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    sub.add_parser("pending")
    for name in ("approve", "deny"):
        p = sub.add_parser(name)
        p.add_argument("approval_id")
    a = sub.add_parser("audit")
    a.add_argument("-n", type=int, default=20)
    args = parser.parse_args(argv)

    s = Settings()
    store = ApprovalStore(s.approvals_file, s.approval_ttl_seconds)

    if args.cmd in (None, "serve"):
        from .server import main as serve
        serve()
        return 0
    if args.cmd == "pending":
        for r in store.list("pending"):
            print(f"{r['id']}  {r['tool']}  ns={r['namespace']}  args={json.dumps(r['args'])}  "
                  f"why: {'; '.join(r['reasons'])}")
        return 0
    if args.cmd in ("approve", "deny"):
        try:
            r = store.decide(args.approval_id, approve=args.cmd == "approve")
        except ApprovalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"{r['id']} {r['status']} by {r['decided_by']}: {r['tool']} ns={r['namespace']} {json.dumps(r['args'])}")
        return 0
    if args.cmd == "audit":
        for e in AuditLog(s.audit_file).tail(args.n):
            why = "; ".join(e.get("reasons", []))
            print(f"{e['ts']}  {e['outcome']:<17} {e['tool']:<20} ns={e['namespace']:<12} "
                  f"{json.dumps(e.get('args', {}))}  {why}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
