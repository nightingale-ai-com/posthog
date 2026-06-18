#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

upstream_remote="${UPSTREAM_REMOTE:-upstream}"
origin_remote="${ORIGIN_REMOTE:-origin}"
upstream_branch="${UPSTREAM_BRANCH:-master}"
target_branch="${1:-$(git branch --show-current)}"

if [[ -z "$target_branch" ]]; then
    echo "Could not determine the current branch. Pass a target branch explicitly." >&2
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Worktree is dirty. Commit, stash, or discard local changes before syncing." >&2
    exit 1
fi

if ! git remote get-url "$upstream_remote" >/dev/null 2>&1; then
    echo "Missing upstream remote: $upstream_remote" >&2
    exit 1
fi

if ! git remote get-url "$origin_remote" >/dev/null 2>&1; then
    echo "Missing origin remote: $origin_remote" >&2
    exit 1
fi

git fetch "$upstream_remote" "+refs/heads/$upstream_branch:refs/remotes/$upstream_remote/$upstream_branch"
git fetch "$origin_remote" "+refs/heads/$target_branch:refs/remotes/$origin_remote/$target_branch"
git switch "$target_branch"
git merge --ff-only "$origin_remote/$target_branch"
git merge --no-edit "$upstream_remote/$upstream_branch"
git push "$origin_remote" "$target_branch"
