# Auto-Fixed Kubernetes Manifests
This folder contains auto-fixed Kubernetes manifests with resolved violations. A total of 1 attempt was made to fix the violations, with a maximum of 3 attempts allowed. All identified violations have been successfully fixed.

## Violation Summary
The following table summarizes the files, violations, status, and attempts made:
| File | Violation | Status | Attempts Made |
| --- | --- | --- | --- |
| deployment.yaml | CPU limit exceeds 2x the request | Fixed | 1 |
| deployment.yaml | Memory limit exceeds 2x the request | Fixed | 1 |
| deployment.yaml | Missing topologySpreadConstraints | Fixed | 1 |
| job.yaml | Missing resources.limits.cpu | Fixed | 1 |
| job.yaml | Missing resources.limits.memory | Fixed | 1 |
| job.yaml | Missing securityContext.allowPrivilegeEscalation | Fixed | 1 |
| job.yaml | Missing securityContext.runAsNonRoot | Fixed | 1 |
| job.yaml | Using ':latest' image tag | Fixed | 1 |
| job.yaml | Missing required label 'app' | Fixed | 1 |
| job.yaml | Missing required label 'env' | Fixed | 1 |
| pdb.yaml | spec.minAvailable must be at least 1 | Fixed | 1 |
| my-app/templates/deployment.yaml | CPU limit exceeds 2x the request | Fixed | 1 |
| my-app/templates/deployment.yaml | Memory limit exceeds 2x the request | Fixed | 1 |
| my-app/templates/deployment.yaml | Missing securityContext.allowPrivilegeEscalation | Fixed | 1 |
| my-app/templates/deployment.yaml | Missing securityContext.runAsNonRoot | Fixed | 1 |
| my-app/templates/deployment.yaml | Using ':latest' image tag | Fixed | 1 |
| my-app/templates/deployment.yaml | Missing required label 'app' | Fixed | 1 |
| my-app/templates/deployment.yaml | Missing required label 'env' | Fixed | 1 |
| my-app/templates/deployment.yaml | Missing topologySpreadConstraints | Fixed | 1 |
| my-app/templates/pdb.yaml | spec.minAvailable must be at least 1 | Fixed | 1 |

## Policies Enforced
The following policies were enforced to fix the violations:
* resource-limits: Ensures that resource limits are set and do not exceed 2x the requested amount.
* image-tag: Requires that image tags are pinned to a specific version, rather than using ':latest'.
* required-labels: Enforces the presence of required labels, such as 'app' and 'env', on pod templates.
* security-context: Mandates the setting of securityContext.allowPrivilegeEscalation to false and securityContext.runAsNonRoot to true.

## Note on Helm Charts
For Helm charts, value-driven violations (e.g., image tag) are fixed in the values.yaml file, while structural violations are fixed in the template file. This approach ensures that the fixes are properly applied and do not interfere with the chart's functionality. For example, a YAML snippet for a fixed deployment might look like:
```yml
spec:
  template:
    spec:
      containers:
      - name: my-app
        image: nginx:1.23.0
        securityContext:
          allowPrivilegeEscalation: false
          runAsNonRoot: true
```
