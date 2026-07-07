# Publish pipeline — secrets & where CI pulls them

Every credential the public-publish path needs, where it lives, and what consumes
it. Populate the vault placeholders with real values, then the pipeline is ready.

## Credential inventory

| Credential | Location | Consumer | Scope / notes |
|------------|----------|----------|---------------|
| GitHub token + user | **vault** `secret/selfshipyard/selfai/publish/github` → `username`, `token` | `scripts/publish/bootstrap.sh` (creates GitHub repos + configures the GitLab push mirror) | A GitHub PAT with **`repo`** (create/admin repos, configure mirror) + **`write:packages`**. Placeholder seeded 2026-07-04 (`token=REPLACE_ME…`). |
| GHCR push | **none** — GitHub Actions `GITHUB_TOKEN` (automatic) | `.github/workflows/publish-ghcr.yml` | Public GHCR push works with the in-Actions token. **Do not** create a PAT for this. |
| PR validation | **none** | `.github/workflows/pr-validation.yml` | `pull_request` trigger runs secret-less; fork PRs never see secrets. |

Only **one** secret must be provisioned: the GitHub token. Everything else is
tokenless by design.

## How each piece pulls its key

- **Git sync (GitLab → GitHub):** `bootstrap.sh` reads the token from vault and
  configures a GitLab **push mirror** per project (protected branches only). The
  credential then lives in GitLab's mirror config; it is not pulled at runtime.
- **Image publish (→ GHCR):** the reusable Actions workflow logs into `ghcr.io`
  with `${{ secrets.GITHUB_TOKEN }}` — minted per run by GitHub, nothing stored.
- **The publish branch:** `public-alpha` must be a **protected** branch on each
  forge project (so only it mirrors).

## To go live (maintainer)

1. Mint a GitHub PAT (scopes above) for the `selfdothost` bot/org user.
2. Write it to vault (replacing the placeholder):
   ```bash
   bao kv put secret/selfshipyard/selfai/publish/github \
     username=<gh-user> token=<gh-pat>
   ```
3. Run `scripts/publish/scan-public.sh` — must exit clean.
4. Run `scripts/publish/bootstrap.sh --apply` to create repos + mirrors.
5. Mark `public-alpha` protected on each forge project; push to it.

Until step 2, `bootstrap.sh` is a **safe no-op** — it reports what it would do
and exits 0.

## What is NOT in vault (and must not be)

- No GitHub PAT for GHCR — Actions' `GITHUB_TOKEN` covers it.
- No secrets in the mirrored content itself — `context/`, `manifests/`, and the
  yard traefik config are excluded from the public mirror, and
  `scan-public.sh` fails the publish on any leaked secret or yard hostname.
