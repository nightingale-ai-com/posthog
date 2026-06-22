# posthog-tier

Pre-rendered Fleet bundle for the PostHog homedev tier, namespace `posthog`.

`rendered.yaml` is generated from:

```bash
kustomize build infra/k8s/app-posthog/overlays/cortex > infra/k8s/posthog-tier/rendered.yaml
```

The companion Fleet `GitRepo` belongs in `home-dev-infra` and should point at
this path in `nightingale-ai-com/posthog`.
