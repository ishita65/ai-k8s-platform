package main

deny[msg] {
    input.kind == "Deployment"
    not input.spec.template.metadata.labels.app
    msg := sprintf("Deployment '%s': pod template is missing required label 'app'", [input.metadata.name])
}

deny[msg] {
    input.kind == "Deployment"
    not input.spec.template.metadata.labels.env
    msg := sprintf("Deployment '%s': pod template is missing required label 'env'", [input.metadata.name])
}

deny[msg] {
    input.kind == "Job"
    not input.spec.template.metadata.labels.app
    msg := sprintf("Job '%s': pod template is missing required label 'app'", [input.metadata.name])
}

deny[msg] {
    input.kind == "Job"
    not input.spec.template.metadata.labels.env
    msg := sprintf("Job '%s': pod template is missing required label 'env'", [input.metadata.name])
}
