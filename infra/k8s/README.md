# Kubernetes deployment workspace

This directory is reserved for the homedev/Cortex Kubernetes deployment of the
PostHog fork.

Suggested shape:

```text
infra/k8s/
  app-posthog/          # Source kustomize overlay for the homedev Cortex cluster
    base/               # Shared PostHog app/runtime manifests
    overlays/
      cortex/           # posthog namespace, routes, storage, and image tags
  posthog-tier/         # Pre-rendered Fleet bundle consumed by home-dev-infra
  docs/                 # Operational notes and migration records
```

PostHog's upstream repository currently treats Docker Compose hobby deployments
as the supported open-source self-host path and notes that self-hosted
Kubernetes Helm support was sunset. Keep our Kubernetes implementation isolated
here so upstream code can be pulled with minimal conflicts.

The live cluster integration is split across repositories:

- This fork owns the app tier source overlay and rendered `posthog-tier` bundle.
- `home-dev-infra` owns the Fleet `GitRepo` CR that reconciles
  `infra/k8s/posthog-tier` into the homedev Cortex cluster.

After changing `app-posthog`, regenerate the rendered bundle:

```bash
kustomize build infra/k8s/app-posthog/overlays/cortex > infra/k8s/posthog-tier/rendered.yaml
```
