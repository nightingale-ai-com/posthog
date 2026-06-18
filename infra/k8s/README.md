# Kubernetes deployment workspace

This directory is reserved for the homedev/Cortex Kubernetes deployment of the
PostHog fork.

Suggested shape:

```text
infra/k8s/
  base/                 # Shared manifests, Helm wrappers, or kustomize base
  overlays/
    cortex/             # Cortex cluster values, routes, storage, resources
  docs/                 # Operational notes and migration records
```

PostHog's upstream repository currently treats Docker Compose hobby deployments
as the supported open-source self-host path and notes that self-hosted
Kubernetes Helm support was sunset. Keep our Kubernetes implementation isolated
here so upstream code can be pulled with minimal conflicts.
