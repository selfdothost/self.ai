#!/usr/bin/env bash
# Publish bootstrap — one-time setup of the GitLab -> GitHub publish path.
# Reads the GitHub credential from vault, then (per repo) creates the public
# GitHub repo and configures a protected-branch push mirror from the forge.
#
# TOKEN-GATED: with no real credential provisioned, this is a safe no-op — it
# reports what it WOULD do and exits 0 (publish-pipeline R5). Populate the real
# token in vault, then re-run to actually bootstrap.
#
#   Vault path : secret/selfshipyard/selfai/publish/github {username, token}
#   Prereqs    : bao (OpenBao) + a sourced vault token; glab; gh (or curl)
#
# Usage: scripts/publish/bootstrap.sh [--apply]
set -euo pipefail

APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

VAULT_PATH="secret/selfshipyard/selfai/publish/github"
PROTECTED_BRANCH="public-alpha"

# Repos to publish under selfdothost (crew-code excluded). See docs/selfdothost-repos.md.
# self.faster-whisper (STT) and self.kokoro-fastapi (TTS) are STRETCH / post-alpha:
# excluded from the first public mirror. Their submodule entries are mirror-excluded
# alongside context/ + manifests/ (see the recursive-clone note in selfdothost-repos.md).
REPOS=(self.ai self.chat self.code-eval self.language-eval \
       self.curator self.llamolotl)

# --- read credential from vault --------------------------------------------
GH_USER=""; GH_TOKEN=""
if command -v bao >/dev/null 2>&1 && [ -n "${BAO_ADDR:-${VAULT_ADDR:-}}" ]; then
  GH_USER=$(bao kv get -field=username "$VAULT_PATH" 2>/dev/null || true)
  GH_TOKEN=$(bao kv get -field=token "$VAULT_PATH" 2>/dev/null || true)
fi

# --- gate: no real token => no-op ------------------------------------------
case "$GH_TOKEN" in
  ""|REPLACE_ME*)
    echo "No GitHub token provisioned at $VAULT_PATH (still a placeholder)."
    echo "Nothing published. Populate the real PAT + username in vault, then re-run with --apply."
    echo "Would bootstrap: ${REPOS[*]}"
    exit 0
    ;;
esac

echo "Credential found for GitHub user '${GH_USER}'."
[ "$APPLY" -eq 1 ] || { echo "Dry run (pass --apply to execute). Would bootstrap: ${REPOS[*]}"; exit 0; }

# --- per-repo bootstrap (only reached with a real token + --apply) ---------
for repo in "${REPOS[@]}"; do
  echo "== $repo =="
  # 1) create the public GitHub repo if missing
  if ! curl -fsS -H "Authorization: Bearer $GH_TOKEN" \
        "https://api.github.com/repos/selfdothost/${repo}" >/dev/null 2>&1; then
    curl -fsS -X POST -H "Authorization: Bearer $GH_TOKEN" \
      https://api.github.com/orgs/selfdothost/repos \
      -d "{\"name\":\"${repo}\",\"private\":false,\"has_issues\":true}" >/dev/null
    echo "  created selfdothost/${repo}"
  else
    echo "  selfdothost/${repo} exists"
  fi
  # 2) configure the GitLab push mirror (protected branches only) to GitHub
  #    via HTTPS using the token. glab must target the forge project.
  glab api --method POST "projects/:id/remote_mirrors" \
    -f "url=https://${GH_USER}:${GH_TOKEN}@github.com/selfdothost/${repo}.git" \
    -f "enabled=true" -f "only_protected_branches=true" \
    -R "selfshipyard/selfai/${repo}" >/dev/null 2>&1 \
    && echo "  push mirror configured (protected branches only)" \
    || echo "  ! mirror config skipped/failed for ${repo} (check project path / permissions)"
done
echo "Bootstrap complete. Ensure '${PROTECTED_BRANCH}' is a protected branch on each forge project."
