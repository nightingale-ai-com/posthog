# PostHog app tier

This app tier runs the `nightingale-ai-com/posthog` fork as a co-tenant workload
on the homedev Cortex Kubernetes cluster.

The `cortex` overlay owns the `posthog` namespace and keeps all PostHog runtime
dependencies in that namespace: CNPG Postgres, Redis, Redpanda, ClickHouse,
SeaweedFS, Browserless, Temporal, web, workers, ingestion, capture, feature
flags, and personhog.

Secrets are read from the shared `openbao-backend` `ClusterSecretStore`.
Seed these OpenBao paths before reconciling the tier:

```text
secret/posthog/postgres        username, password
secret/posthog/app             secret_key, encryption_salt_keys, browserless_token
secret/posthog/objectstorage   root_user, root_password
```

Render the Fleet bundle after changing this source overlay:

```bash
kustomize build infra/k8s/app-posthog/overlays/cortex > infra/k8s/posthog-tier/rendered.yaml
```

The public host is `posthog.nightingale-ai.com`; Traefik path routing mirrors the
hobby Caddy routing for capture, replay capture, logs/metrics/traces, feature
flags, surveys, webhooks, livestream, object storage, and the web app.
