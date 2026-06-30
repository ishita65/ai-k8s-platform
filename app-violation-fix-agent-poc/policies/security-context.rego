package main

# Helper: container has runAsNonRoot: true
_run_as_non_root(container) {
    container.securityContext.runAsNonRoot == true
}

# Helper: container has allowPrivilegeEscalation: false
_no_privilege_escalation(container) {
    container.securityContext.allowPrivilegeEscalation == false
}

deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    not _run_as_non_root(container)
    msg := sprintf("Deployment '%s': container '%s' must set securityContext.runAsNonRoot: true", [input.metadata.name, container.name])
}

deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    not _no_privilege_escalation(container)
    msg := sprintf("Deployment '%s': container '%s' must set securityContext.allowPrivilegeEscalation: false", [input.metadata.name, container.name])
}

deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    not _run_as_non_root(container)
    msg := sprintf("Job '%s': container '%s' must set securityContext.runAsNonRoot: true", [input.metadata.name, container.name])
}

deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    not _no_privilege_escalation(container)
    msg := sprintf("Job '%s': container '%s' must set securityContext.allowPrivilegeEscalation: false", [input.metadata.name, container.name])
}
