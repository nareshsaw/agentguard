package agentguard_test

import rego.v1

import data.agentguard

ctx := {"approved": false, "recent_mutations": 0, "change_freeze": false}

call(tool, ns, args, extra) := {"tool": tool, "namespace": ns, "args": args, "context": object.union(ctx, extra)}

decide(i) := d if {
	d := agentguard.decision with input as i
}

test_reads_are_allowed_everywhere if {
	d := decide(call("list_pods", "kube-system", {}, {}))
	d.allow
	not d.require_approval
}

test_unknown_tool_is_denied if {
	d := decide(call("exec_into_pod", "staging", {}, {}))
	not d.allow
	d.deny_reasons == ["tool 'exec_into_pod' is not on the allow-list"]
}

test_staging_restart_is_allowed if {
	d := decide(call("restart_deployment", "staging", {"name": "web"}, {}))
	d.allow
}

test_protected_namespace_is_denied if {
	d := decide(call("delete_pod", "kube-system", {"pod": "coredns-1"}, {}))
	not d.allow
	not d.require_approval
	d.deny_reasons == ["namespace 'kube-system' is protected: agents may not change it"]
}

test_prod_change_needs_approval if {
	d := decide(call("restart_deployment", "prod", {"name": "web"}, {}))
	not d.allow
	d.require_approval
}

test_prod_change_with_approval_is_allowed if {
	d := decide(call("restart_deployment", "prod", {"name": "web"}, {"approved": true}))
	d.allow
	not d.require_approval
}

test_approval_cannot_override_a_deny if {
	d := decide(call("scale_deployment", "prod", {"name": "web", "replicas": 50}, {"approved": true, "current_replicas": 2}))
	not d.allow
	not d.require_approval
}

test_scale_over_max_is_denied if {
	d := decide(call("scale_deployment", "staging", {"name": "web", "replicas": 11}, {"current_replicas": 8}))
	not d.allow
	"11 replicas exceeds the limit of 10" in d.deny_reasons
}

test_scale_more_than_2x_is_denied if {
	d := decide(call("scale_deployment", "staging", {"name": "web", "replicas": 5}, {"current_replicas": 2}))
	not d.allow
	"scaling from 2 to 5 is more than 2x in one step" in d.deny_reasons
}

test_scale_2x_is_allowed if {
	d := decide(call("scale_deployment", "staging", {"name": "web", "replicas": 4}, {"current_replicas": 2}))
	d.allow
}

test_prod_scale_to_zero_is_denied if {
	d := decide(call("scale_deployment", "prod", {"name": "web", "replicas": 0}, {"current_replicas": 2, "approved": true}))
	not d.allow
	"scaling a 'prod' workload to zero is not allowed" in d.deny_reasons
}

test_big_scale_down_needs_approval if {
	d := decide(call("scale_deployment", "staging", {"name": "web", "replicas": 1}, {"current_replicas": 4}))
	not d.allow
	d.require_approval
}

test_rate_limit_blocks_changes if {
	d := decide(call("restart_deployment", "staging", {"name": "web"}, {"recent_mutations": 5}))
	not d.allow
}

test_rate_limit_does_not_block_reads if {
	d := decide(call("get_events", "staging", {}, {"recent_mutations": 50}))
	d.allow
}

test_change_freeze_blocks_changes if {
	d := decide(call("rollback_deployment", "staging", {"name": "web"}, {"change_freeze": true}))
	not d.allow
	d.deny_reasons == ["a change freeze is active: no changes are allowed"]
}
