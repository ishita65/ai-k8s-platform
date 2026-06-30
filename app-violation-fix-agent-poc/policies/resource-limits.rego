package main

deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    not container.resources.limits.cpu
    msg := sprintf("Deployment '%s': container '%s' is missing resources.limits.cpu", [input.metadata.name, container.name])
}

deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    not container.resources.limits.memory
    msg := sprintf("Deployment '%s': container '%s' is missing resources.limits.memory", [input.metadata.name, container.name])
}

deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    not container.resources.limits.cpu
    msg := sprintf("Job '%s': container '%s' is missing resources.limits.cpu", [input.metadata.name, container.name])
}

deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    not container.resources.limits.memory
    msg := sprintf("Job '%s': container '%s' is missing resources.limits.memory", [input.metadata.name, container.name])
}
