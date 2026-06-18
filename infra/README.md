# PostHog fork infrastructure

This directory is the local ownership boundary for the `nightingale-ai-com/posthog`
fork. Keep Kubernetes, cluster bootstrap, and homedev-specific deployment work
here so the rest of the repository can stay close to `PostHog/posthog`.

## Remote layout

- `origin`: `https://github.com/nightingale-ai-com/posthog.git`
- `upstream`: `https://github.com/PostHog/posthog.git`

## Sync upstream

From a clean worktree:

```bash
infra/scripts/sync-upstream.sh
```

The script fetches `upstream/master`, merges it into the current fork branch,
and pushes the result back to `origin`. This is expected to create merge commits
once local `infra/` changes exist.

## Local conventions

- Put Kubernetes work under `infra/k8s/`.
- Keep generated manifests out of git unless they are the source of truth for
  Fleet or another reconciler.
- Prefer cluster-owned secrets through External Secrets or the target cluster's
  secret manager. Do not commit plaintext credentials.
- Avoid changing vendored PostHog application files for deployment-only needs.
  If an app patch is required, keep it small and document why in this directory.
