# AgentGuard policy: decides whether an AI agent may run a Kubernetes tool call.
#
# Input (built by the AgentGuard server for every tool call):
#   {"tool": "scale_deployment", "namespace": "prod",
#    "args": {"name": "web", "replicas": 4},
#    "context": {"current_replicas": 2, "approved": false,
#                "recent_mutations": 1, "change_freeze": false}}
#
# Output: data.agentguard.decision
#   {"allow": bool, "require_approval": bool, "deny_reasons": [...], "approval_reasons": [...]}
package agentguard

import rego.v1

config := data.config

read_tools := {
	"list_pods", "get_pod_logs", "get_events",
	"describe_deployment", "diagnose_namespace", "get_audit_log",
}

mutating_tools := {"restart_deployment", "scale_deployment", "rollback_deployment", "delete_pod"}

is_mutation if input.tool in mutating_tools

# ---------------------------------------------------------------- hard denies

deny contains msg if {
	not input.tool in read_tools
	not input.tool in mutating_tools
	msg := sprintf("tool '%s' is not on the allow-list", [input.tool])
}

deny contains msg if {
	is_mutation
	input.namespace in config.protected_namespaces
	msg := sprintf("namespace '%s' is protected: agents may not change it", [input.namespace])
}

deny contains "a change freeze is active: no changes are allowed" if {
	is_mutation
	freeze_active
}

freeze_active if config.change_freeze == true

freeze_active if input.context.change_freeze == true

deny contains msg if {
	is_mutation
	input.context.recent_mutations >= config.rate_limit.max_mutations
	msg := sprintf(
		"rate limit reached: %d changes in the last %d seconds (max %d)",
		[input.context.recent_mutations, config.rate_limit.window_seconds, config.rate_limit.max_mutations],
	)
}

deny contains msg if {
	input.tool == "scale_deployment"
	input.args.replicas > config.max_replicas
	msg := sprintf("%d replicas exceeds the limit of %d", [input.args.replicas, config.max_replicas])
}

deny contains msg if {
	input.tool == "scale_deployment"
	current := input.context.current_replicas
	current > 0
	input.args.replicas > current * config.max_scale_factor
	msg := sprintf(
		"scaling from %d to %d is more than %dx in one step",
		[current, input.args.replicas, config.max_scale_factor],
	)
}

deny contains "replicas cannot be negative" if {
	input.tool == "scale_deployment"
	input.args.replicas < 0
}

deny contains msg if {
	input.tool == "scale_deployment"
	input.args.replicas == 0
	input.namespace in config.approval_namespaces
	msg := sprintf("scaling a '%s' workload to zero is not allowed", [input.namespace])
}

# ---------------------------------------------------------- human approval

approval contains msg if {
	is_mutation
	input.namespace in config.approval_namespaces
	msg := sprintf("changes in '%s' need human approval", [input.namespace])
}

approval contains msg if {
	input.tool == "scale_deployment"
	current := input.context.current_replicas
	input.args.replicas < current
	input.args.replicas * 2 < current
	msg := sprintf("scaling down by more than half (%d -> %d) needs human approval", [current, input.args.replicas])
}

approved if input.context.approved == true

# ----------------------------------------------------------------- decision

default allow := false

allow if {
	count(deny) == 0
	count(approval) == 0
}

allow if {
	count(deny) == 0
	approved
}

default require_approval := false

require_approval if {
	count(deny) == 0
	count(approval) > 0
	not approved
}

decision := {
	"allow": allow,
	"require_approval": require_approval,
	"deny_reasons": sort([m | some m in deny]),
	"approval_reasons": sort([m | some m in approval]),
}
