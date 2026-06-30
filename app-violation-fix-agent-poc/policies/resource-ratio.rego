package main

# Parse a CPU quantity string to millicores.
# Handles: "500m" -> 500, "1" -> 1000, "0.5" -> 500
_cpu_millicores(q) = v {
    endswith(q, "m")
    v := to_number(substring(q, 0, count(q) - 1))
}

_cpu_millicores(q) = v {
    not endswith(q, "m")
    v := to_number(q) * 1000
}

# Parse a memory quantity string to mebibytes.
# Handles: "256Mi" -> 256, "1Gi" -> 1024, "256M" -> 256, "1G" -> 1000
_memory_mebibytes(q) = v {
    endswith(q, "Mi")
    v := to_number(substring(q, 0, count(q) - 2))
}

_memory_mebibytes(q) = v {
    endswith(q, "Gi")
    v := to_number(substring(q, 0, count(q) - 2)) * 1024
}

_memory_mebibytes(q) = v {
    endswith(q, "M")
    not endswith(q, "Mi")
    v := to_number(substring(q, 0, count(q) - 1))
}

_memory_mebibytes(q) = v {
    endswith(q, "G")
    not endswith(q, "Gi")
    v := to_number(substring(q, 0, count(q) - 1)) * 1000
}

# Deployment: CPU limit must not exceed 2x CPU request
deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    req := _cpu_millicores(container.resources.requests.cpu)
    lim := _cpu_millicores(container.resources.limits.cpu)
    lim > req * 2
    msg := sprintf("Deployment '%s': container '%s' CPU limit (%s) exceeds 2x the request (%s) — reduce limit to at most %vm",
                   [input.metadata.name, container.name,
                    container.resources.limits.cpu, container.resources.requests.cpu,
                    req * 2])
}

# Deployment: memory limit must not exceed 2x memory request
deny[msg] {
    input.kind == "Deployment"
    container := input.spec.template.spec.containers[_]
    req := _memory_mebibytes(container.resources.requests.memory)
    lim := _memory_mebibytes(container.resources.limits.memory)
    lim > req * 2
    msg := sprintf("Deployment '%s': container '%s' memory limit (%s) exceeds 2x the request (%s) — reduce limit to at most %vMi",
                   [input.metadata.name, container.name,
                    container.resources.limits.memory, container.resources.requests.memory,
                    req * 2])
}

# Job: CPU limit must not exceed 2x CPU request
deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    req := _cpu_millicores(container.resources.requests.cpu)
    lim := _cpu_millicores(container.resources.limits.cpu)
    lim > req * 2
    msg := sprintf("Job '%s': container '%s' CPU limit (%s) exceeds 2x the request (%s) — reduce limit to at most %vm",
                   [input.metadata.name, container.name,
                    container.resources.limits.cpu, container.resources.requests.cpu,
                    req * 2])
}

# Job: memory limit must not exceed 2x memory request
deny[msg] {
    input.kind == "Job"
    container := input.spec.template.spec.containers[_]
    req := _memory_mebibytes(container.resources.requests.memory)
    lim := _memory_mebibytes(container.resources.limits.memory)
    lim > req * 2
    msg := sprintf("Job '%s': container '%s' memory limit (%s) exceeds 2x the request (%s) — reduce limit to at most %vMi",
                   [input.metadata.name, container.name,
                    container.resources.limits.memory, container.resources.requests.memory,
                    req * 2])
}
