package main

deny[msg] {
    input.kind == "PodDisruptionBudget"
    not input.spec.minAvailable >= 1
    msg := sprintf("PodDisruptionBudget '%s': spec.minAvailable must be at least 1 (got %v)", [input.metadata.name, input.spec.minAvailable])
}
