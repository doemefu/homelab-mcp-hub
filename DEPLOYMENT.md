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
3. Owner: add the required status checks (`lint-and-test`, `image-smoke`, `providers`, `Analyze (python)`) and the CodeQL rule to the ruleset `main` (done: the ruleset requires all four checks).
4. Owner: create a write deploy key for this repository and the Secret `mcp-hub-flux-auth` in `flux-system` (private key never through chat).
5. `doemefu/homelab` platform pull request (WP6): Flux bundle `cluster/apps/mcp-hub/`, playbook 59 writes Secret `mcp-hub-secrets` (`accounts.json`, `allowed-subjects`, credential keys).
6. Flux replaces the placeholder image tag in `k8s/deployment.yaml` (it was never built) with the newest `main-<ts>` tag and pushes an image-update commit to `main`. The ruleset `main` requires pull requests, with bypass for the admin role only; that first Flux commit proves the push works. If image automation reports a rejected push, the owner adds the deploy key as a bypass actor.
7. The tunnel route for `mcp.furchert.ch` → port 8083 is merged last, after the hub is Ready.

## Operations

**Registry changes.** `accounts.json` is read only at start-up. The Deployment uses `Recreate`, and an invalid registry makes the new pod exit with code 2 and one `startup_failed` line (it restarts until the registry is fixed), which would take every account down. So, after the Secret changes, **always run the registry check before deleting the pod** (spec 080 §9.6, D66), with the hash computed from the Secret's own `accounts.json`:

```bash
S=$(kubectl -n apps get secret mcp-hub-secrets -o jsonpath='{.data.accounts\.json}' | base64 -d | shasum -a 256 | cut -c1-12)
kubectl -n apps exec deploy/mcp-hub -c mcp-hub -- mcp-hub check-registry --expect-sha "$S"; echo "exit $?"
kubectl -n apps delete pod -l app=mcp-hub   # only after exit 0
```

`check-registry` exit codes: 0 every check passed; 1 a check failed (`registry error <path>: <type>`, `… missing <key>`, `key invalid`, `token unreachable`, or "not the expected one yet": the kubelet has not refreshed the volume — wait a minute and run it again); 2 usage or configuration error. A token state `none` (no login yet) is not a failure. Output lines are listed in INTERFACES.md §7.

**Own addresses (owner, optional).** `mail.own_addresses_ref` names a credential file with the owner's own addresses of a mail account (spec 080 §8.2, D70). Order, because an older hub rejects the unknown registry field and does not start (R36): first an image with this field runs in the cluster (`kubectl -n apps get pod -l app=mcp-hub` shows the new image); only then add the files (for example `icloud-own-addresses`) to `mcp_hub_credentials` and `mail.own_addresses_ref` to the registry in SOPS (the owner edits SOPS); then playbook 59, `mcp-hub check-registry --expect-sha …` (it prints `account <id> own_addresses=<n>`; `invalid` means a missing or malformed file, which does not fail the check or disable the account), and the pod deletion (D51, D66). A changed address list later needs only playbook 59: the file is read on every use.

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

**Resources.** The container's CPU limit is 1 core: calendar expansion is single-threaded and CPU-bound, and at 500m one busy worker would run at half speed in wall time (5 s expansion budget, 20 s call timeout). Memory limit 256 Mi; measured in this image under these limits (Linux/arm64, `scripts/memory_probe.py`), one account stays at or below 165 MiB with no OOM kill; two accounts queried concurrently with emoji-heavy time-zone data reach 256 MiB (INTERFACES.md, memory table; spec 080 rev. 4.5 D62). A maximal Graph message (2 MiB answer, 1 MiB attachment list) next to the 240 KiB non-ASCII calendar scenario, 10 calls, measured on 2026-10-05: cgroup `memory.peak` 162–180 MiB and process peak RSS 181–182 MiB over three runs, no OOM kill (gate: cgroup `memory.peak` ≤ 230 MiB). The cgroup figure also counts page cache charged to the container and varies between runs and hosts; one earlier run reached 226.8 MiB. `get_message_status` (hostile IMAP scenario, 2 accounts x 2 concurrent calls) next to 2 concurrent `get_events` accounts: cgroup `memory.peak` 191 MiB, no OOM kill (gate 230 MiB; INTERFACES.md memory table, 2026-10-22). `get_thread` (hostile IMAP `thread` scenario with 10,000-part BODYSTRUCTUREs, 2 accounts x 2 concurrent calls): cgroup `memory.peak` 145-189 MiB alone and 218-240 MiB next to the calendar scenario; the Graph `thread-graph` scenario 145-151 MiB alone and 217-225 MiB with it; no OOM kill (gate 245 MiB, owner decision 2026-10-23; INTERFACES.md, 2026-10-23). The image sets `MALLOC_ARENA_MAX=2`.

**Enabling `outlook` (owner).** The order is fixed by the infrastructure runbook "mcp-hub token store and the Outlook account (#171 onboarding)" in `DEPLOYMENT.md` of `doemefu/homelab`: this repository's Graph mail adapter is merged and rolled out by Flux; SOPS gets the token-store variables and the `outlook` registry entry with its client id; playbook 59 creates the database, the role and the Secret keys; `mcp-hub check-registry --expect-sha …` passes; the pod is deleted; `list_accounts` shows `outlook` mail `auth_expired` (no token yet, expected); then the Graph login below; then a `list_unread` and one `get_message` for `outlook`.

**Enabling `gmail` (owner).** Configuration only, no new image: the infrastructure runbook "mcp-hub: Gmail account (#172)" in `DEPLOYMENT.md` of `doemefu/homelab` adds the credentials and the registry entry through SOPS and playbook 59, then `mcp-hub check-registry --expect-sha …` and a pod deletion. `check-registry` validates the configuration only and does not log in; the first `list_unread` for `gmail` is the real login test.

**Graph login (owner).** Once the token-store keys (`db-username`, `db-password`, `token-encryption-key`) are in `mcp-hub-secrets` and a Graph account (`outlook`) is enabled in the registry, the owner signs in once with the device code (spec 080 [§7.3](https://github.com/doemefu/homelab/blob/main/docs/080-mcp-hub.md); a cluster action, so it needs the owner's go):

```bash
kubectl -n apps exec -it deploy/mcp-hub -- mcp-hub login outlook
# off the LAN, as a one-shot over SSH:
ssh -t -i ~/.ssh/homelab -o IdentitiesOnly=yes -o ProxyCommand="cloudflared access ssh --hostname %h" ansible@ssh.furchert.ch 'sudo k3s kubectl -n apps exec -it deploy/mcp-hub -- mcp-hub login outlook'
```

- `-it` is required: the command refuses to run without a terminal, so the sign-in code never lands in a collected log.
- It prints a Microsoft address and a code. Open the address in a private browser window, enter the code, and before approving check that the app name is yours and that only mail read and "Maintain access" are requested.
- Exit codes: 0 stored; 1 declined, expired, timed out, refused by Microsoft, token endpoint unreachable or not stored (run it again; a "refused" message names the app-registration settings to check); 2 usage or configuration error (no terminal, unknown account, not a Graph account, token store not configured).
- Re-login: the same command whenever the account shows `auth_expired`; it replaces the stored token and clears a recorded `invalid_grant`. `list_accounts` shows the new status at the next status check or the next call.
- Key rotation: key ids are derived from the key bytes, so a rotation needs no change to these manifests; the procedure (old key → `token-encryption-key-previous`, new key → `token-encryption-key`, remove the previous key only after every Graph row is under the current key) is in the infrastructure runbook of `doemefu/homelab`.

**Verification**

```bash
scripts/smoke_image.sh <image>                     # locally, before a release
kubectl -n apps get pods -l app=mcp-hub            # after a deploy
kubectl -n apps logs deployment/mcp-hub --tail=50
```

After a registry change that enables an account (stage b, spec §11.1): the background status check runs about 30 s after the pod start (`HUB_STATUS_CHECK_ENABLED=true` in `k8s/deployment.yaml`), so `list_accounts` shows `ok` for each working capability shortly after that; until then it shows `unknown`. Each cycle writes one `status_check_cycle` line (`result_count`, `outcome`).

After the WP13c rollout (owner, in claude.ai), per mail account: pick a thread that holds an own reply, find it with `list_messages` (`folder: "all"`) and call `get_thread` with its `ref`. Expect the own reply as `outgoing`, no quoted history in the bodies and an unchanged read state in the mailbox. For `outlook` also check the log:

```bash
kubectl -n apps logs -l app=mcp-hub --since=15m | grep provider_call_failed
```

No line means Graph accepts `uniqueBody` on list requests (spec 080 assumption O37). A line is expected behaviour (the list is read again without `uniqueBody`): record its `outcome` and `exception` on story #44.

## Rollback

Revert the Flux image-update commit on `main` (or the offending code commit) through a pull request; Flux applies the previous tag. The hub keeps no state apart from in-memory status, so a rollback needs no data migration. Before rolling back to an image older than WP13a, remove `mail.own_addresses_ref` from the registry (SOPS, playbook 59), because the older hub rejects the field and does not start. Ids of folders other than the inbox and all cursors stop working after the rollback. Rolling back below WP13b removes `get_message_status`; a routine that calls it fails with an unknown tool. Rolling back below WP13c removes `get_thread`.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Every token gets 401 | `token_rejected` lines: the `check` value names the failing rule (`signature` with `jwks_fetch_failed` → auth-service JWKS unreachable; `subject` → `allowed-subjects` empty or missing, see `allowlist_empty` / `allowlist_unavailable`) |
| An account shows `disabled` | `credential_missing` warnings at start-up name the missing or unreadable key; if every credential is unreadable, try `defaultMode: 0440` on the Secret volume |
| Pod restarts with exit code 2 | `startup_failed` line: `reason` names the invalid variable or registry field |
| A Graph account shows `auth_expired` | `token_refresh` lines for that `account`: `no_token` (never signed in), `invalid_grant` / `invalid_grant_recorded` (Microsoft revoked the grant, also after the app's access is removed in the Microsoft account) or `decrypt_failed` (key changed without rotation) → run the Graph login above |
| A Graph account shows `error` | `token_refresh` with `outcome=error` or `persist_failed`: `exception` names the cause (`KeyUnreadable` = key file missing, unreadable or not strict base64, `KeyLength` = the key is not exactly 32 bytes, `SameKey` = current and previous key are identical; `TokenStoreNotConfigured` = a token-store file is missing; `OperationalError` = the connection failed — with psycopg 3.3.6 a missing database `mcp_hub`, wrong credentials and an unreachable server all show as this cause, the driver does not distinguish them; `ClientRejected` = app registration refused; `ConnectError`, `ConnectTimeout`, `TokenEndpointStatus` and similar = Microsoft's token endpoint unreachable, slow or failing; `CallDeadline` / `TokenLock` = the call ran out of time before or while waiting for a refresh) |
| A Graph account shows `error` after a call | `provider_call_failed` / `status_check_failed` with `exception`: `Forbidden` = Graph answered 403 (a permission or licence problem on the Microsoft side; a re-login does not help, check the app registration's delegated permissions and the account), `Throttled` = Graph answered 429 or 503 and calls fail fast until `Retry-After` (at most 5 minutes) has passed, `Redirect`, `ContentEncoding` or `MalformedJson` = an unexpected Graph answer, `NoInboxId` (status check) or `MalformedFolderId` = a well-known folder id could not be read |
| A new Graph setup shows `error` with `OperationalError` or `DatabaseMissing` | The token-store database or role does not exist yet: run playbook 59 of `doemefu/homelab` (it creates `mcp_hub`), then the Graph login |
| `get_message` answers `not_found` for an `outlook` message | The message was deleted, is a draft, or its parent folder is junk, drafts or outbox (a moved message is fine), or the id belongs to another account |
| `get_message` by `ref` answers `upstream_timeout` | The search over all folders did not complete (soft stop, folder cap, failed batch); the message may still exist. Retry or use the `id` from a list result |
| `get_message_status` answers `incomplete_search` or `thread_checked: false` for some refs while the account stays `ok` | A cap, the 16 s soft stop or a failed batch left those refs unchecked; ask again for only those refs. On a Graph account a failed lookup batch logs a `provider_call_failed` WARNING (`exception` = cause class, for example `Throttled`), also when the call still answers; only the first failed batch of each `outcome`/`exception` class is logged per call (at most 20 lines), so 50 failing reads write one line, and after a `Throttled` answer the later batches of that call fail without a request. IMAP batches answered `NO` are not logged |
| `get_thread` for an `outlook` thread returns `body_omitted: true` on older messages, with one `provider_call_failed` WARNING | Graph refused or could not serve the list with `uniqueBody` (`outcome`/`exception` name the cause), so the list was read again without it and at most 20 bodies were read one by one, newest first; the others stay omitted. Read them with `get_message` by `ref` |
| `get_thread` answers `upstream_timeout` | The seed lookup did not finish, or a key search found nothing and did not finish (on Graph also a soft stop after the seed lookup, before the list answered); the thread state is unknown. Retry. A search that found members returns them with `truncated: true` instead |
| `/readyz` 503 | The process has not finished start-up; check the log for `startup_failed` |
| An account shows `auth_expired` | `status_check_failed` line with `outcome=auth_expired` for that `account` and `capability`: the provider rejected the credential (for iCloud: create a new app-specific password, update the credential in SOPS, run playbook 59, delete the pod) |
| An account shows `unreachable` or `error` | `status_check_failed` / `provider_call_failed` lines: `unreachable` = connection or timeout (egress, provider outage), `error` with `outcome=too_large` = a provider answer above 5 MiB (narrow `include_calendars`), other `upstream_error` = an unexpected provider answer |
| An event is missing from `get_events` | `calendar_object_skipped` lines: that calendar object could not be parsed and was skipped on its own |
| 421 or 403 without `WWW-Authenticate` | Request `Host` is not `mcp.furchert.ch` or `Origin` is not allowed (`request` line with `check` `host` / `origin`) |
