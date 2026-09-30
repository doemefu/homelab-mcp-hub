# Deployment

The hub runs as one pod in namespace `apps`, deployed by Flux from this repository's `k8s/` directory. The Flux objects, the Secret and the tunnel route live in the `doemefu/homelab` repository ([doemefu/homelab#177](https://github.com/doemefu/homelab/issues/177)).

## Manifests

| File | Content |
|------|---------|
| `k8s/deployment.yaml` | Deployment `mcp-hub`: 1 replica, `Recreate`, uid/gid 10001, read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp, no service-account token, probes on port 8084, Secret `mcp-hub-secrets` mounted as a whole volume at `/etc/mcp-hub/secrets` (`defaultMode: 0400`), `emptyDir` at `/tmp` |
| `k8s/service.yaml` | ClusterIP Service `mcp-hub`: ports `8083` (`mcp`) and `8084` (`internal`) |
| `k8s/kustomization.yaml` | Lists both |

Resources are estimates (requests 50m / 128Mi, limits 500m / 256Mi); confirm with `kubectl top` after the first deploy. The manifests pass the policies in `doemefu/homelab` `policy/kubernetes/` (`conftest`, see `.claude/rules/commands.md`).

## Image

- `ghcr.io/doemefu/homelab-mcp-hub`, built by `.github/workflows/build.yml` on every push to `main` (except `k8s/**` changes).
- Native builds on `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64), pushed by digest and merged into one multi-arch index; the merge job refuses a partial index and verifies both platforms.
- Tags: the short commit SHA and `main-YYYYMMDDTHHmmss` (Flux policy `^main-[0-9]{8}T[0-9]{6}$`). No `latest`.
- Base images pinned by tag and index digest; the app runs from source (`PYTHONPATH=/app/src`, `python -m mcp_hub`) as uid 10001.

## Bootstrap sequence

1. Merge the bootstrap pull request; the first `Build and Push` run on `main` publishes the first image.
2. Owner: GitHub → Packages → `homelab-mcp-hub` → visibility **Public**; confirm it is linked to this repository.
3. Owner: add the required status checks (`lint-and-test`, `image-smoke`, `Analyze (python)`) and the CodeQL rule to the ruleset `main` once GitHub knows the check names.
4. Owner: create a write deploy key for this repository and the Secret `mcp-hub-flux-auth` in `flux-system` (private key never through chat).
5. `doemefu/homelab` platform pull request (WP6): Flux bundle `cluster/apps/mcp-hub/`, playbook 59 writes Secret `mcp-hub-secrets` (`accounts.json`, `allowed-subjects`, credential keys).
6. Flux replaces the placeholder image tag in `k8s/deployment.yaml` (it was never built) with the newest `main-<ts>` tag and pushes an image-update commit to `main`. The ruleset `main` requires pull requests, with bypass for the admin role only; that first Flux commit proves the push works. If image automation reports a rejected push, the owner adds the deploy key as a bypass actor.
7. The tunnel route for `mcp.furchert.ch` → port 8083 is merged last, after the hub is Ready.

## Operations

**Registry changes.** `accounts.json` is read only at start-up. After the Secret changes, delete the pod:

```bash
kubectl -n apps delete pod -l app=mcp-hub
```

An invalid registry makes the new pod exit with code 2 and one `startup_failed` line (it restarts until the registry is fixed).

**Kill switch** (spec §4.6 layer L2, two steps; owner go, because both change the cluster or SOPS):

1. Immediately:

   ```bash
   kubectl -n apps patch secret mcp-hub-secrets --type merge -p '{"stringData":{"allowed-subjects":""}}'
   kubectl -n apps delete pod -l app=mcp-hub
   ```

   The new pod reads the empty file at start-up and rejects every token (target: at most 2 minutes from the patch to the first refused call). The pod deletion also closes any open `GET /mcp` stream. If the deletion is skipped, the running pod still rejects every token once the empty file reaches it and is re-read (at most every 60 s): this is the backstop.
2. Then set `mcp_hub_allowed_subjects: []` in SOPS in the `doemefu/homelab` repository. **Until this is done, any playbook-59 run rewrites the Secret from SOPS and restores access.**

Re-enabling reverses both steps (SOPS first, then playbook 59 or a patch). The full incident runbook is in `DEPLOYMENT.md` of the `doemefu/homelab` repository (section "mcp-hub incident runbook").

**Signing-key revocation.** The hub caches auth-service's signing keys and re-reads them at most every hour; a key that auth-service withdraws stops being accepted at the hub within 1 hour, or immediately after `kubectl -n apps delete pod -l app=mcp-hub`.

**Verification**

```bash
scripts/smoke_image.sh <image>                     # locally, before a release
kubectl -n apps get pods -l app=mcp-hub            # after a deploy
kubectl -n apps logs deployment/mcp-hub --tail=50
```

## Rollback

Revert the Flux image-update commit on `main` (or the offending code commit) through a pull request; Flux applies the previous tag. The hub keeps no state apart from in-memory status, so a rollback needs no data migration.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Every token gets 401 | `token_rejected` lines: the `check` value names the failing rule (`signature` with `jwks_fetch_failed` → auth-service JWKS unreachable; `subject` → `allowed-subjects` empty or missing, see `allowlist_empty` / `allowlist_unavailable`) |
| An account shows `disabled` | `credential_missing` warnings at start-up name the missing or unreadable key; if every credential is unreadable, try `defaultMode: 0440` on the Secret volume |
| Pod restarts with exit code 2 | `startup_failed` line: `reason` names the invalid variable or registry field |
| `/readyz` 503 | The process has not finished start-up; check the log for `startup_failed` |
| 421 or 403 without `WWW-Authenticate` | Request `Host` is not `mcp.furchert.ch` or `Origin` is not allowed (`request` line with `check` `host` / `origin`) |
