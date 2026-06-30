package main

deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    endswith(container.image, ":latest")
    msg := sprintf("Deployment '%s': container '%s' uses ':latest' image tag — pin to a specific version (image: %s)", [input.metadata.name, container.name, container.image])
}

deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    not contains(container.image, ":")
    msg := sprintf("Deployment '%s': container '%s' has no image tag — pin to a specific version (image: %s)", [input.metadata.name, container.name, container.image])
}

deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    endswith(container.image, ":latest")
    msg := sprintf("Job '%s': container '%s' uses ':latest' image tag — pin to a specific version (image: %s)", [input.metadata.name, container.name, container.image])
}

deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    not contains(container.image, ":")
    msg := sprintf("Job '%s': container '%s' has no image tag — pin to a specific version (image: %s)", [input.metadata.name, container.name, container.image])
}
