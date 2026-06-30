package main

deny[msg] {
    input.kind == "Deployment"
    not _has_topology_spread
    msg := sprintf("Deployment '%s': spec.template.spec.topologySpreadConstraints must be defined for best-effort node spreading", [input.metadata.name])
}

_has_topology_spread {
    count(input.spec.template.spec.topologySpreadConstraints) > 0
}
