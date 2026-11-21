# Read-only identities

Every server carries its own credential, and that credential is read-only
on the target system, except the OpenGist token, the Docmost account and
the SilverBullet token (see [OpenGist](#opengist), [Docmost](#docmost) and
[SilverBullet](#silverbullet)). The server code is the
second line of defence, not the first. Store each secret as one file under
`~/.config/homelab-mcp/secrets/` with mode 600. Never paste a secret into a
command line, a doc, or Claude Code settings.

```bash
mkdir -p -m 700 ~/.config/homelab-mcp/secrets
```

Copy each secret to the clipboard and write it with `pbpaste`, so the
value never lands in shell history:

```bash
pbpaste > ~/.config/homelab-mcp/secrets/pve && chmod 600 ~/.config/homelab-mcp/secrets/pve
```

## Proxmox VE

On `pve`:

```bash
pveum user add mcp@pve --comment "homelab MCP, read-only"
pveum acl modify / --users mcp@pve --roles PVEAuditor
pveum user token add mcp@pve readonly --privsep 0
```

The last command prints the secret once. `--privsep 0` makes the token
inherit exactly the user's PVEAuditor rights. Token id: `mcp@pve!readonly`.

## Proxmox Backup Server

On `pbs`:

```bash
proxmox-backup-manager user create mcp@pbs --comment "homelab MCP, read-only"
proxmox-backup-manager user generate-token mcp@pbs readonly
proxmox-backup-manager acl update / Audit --auth-id 'mcp@pbs'
proxmox-backup-manager acl update / Audit --auth-id 'mcp@pbs!readonly'
```

A PBS token's rights are the intersection of its own ACL and its user's,
so both ACL lines are needed. Audit can list datastores, groups,
snapshots and tasks but cannot read backup content or restore.

The firewall only allows pve to reach PBS on 8007. The Mac in the Admin
zone reaches it through the Admin-to-everything rule; a host elsewhere needs
an allow in `proxmox-infra/terraform/unifi/policy_matrix.tf`.

## Unraid

On `tower` (Unraid 7.2 or later):

```bash
unraid-api apikey --create --name "homelab mcp" --roles VIEWER --json
```

If the API rejects `VIEWER` (older API), use explicit read permissions:

```bash
unraid-api apikey --create --name "homelab mcp" --json --permissions \
  "ARRAY:READ_ANY,DOCKER:READ_ANY,VMS:READ_ANY,SHARE:READ_ANY,INFO:READ_ANY,NOTIFICATIONS:READ_ANY"
```

Keep the GraphQL sandbox off; the server does not need introspection.

## Technitium (ns1 and ns2)

In the ns1 web UI, Administration:

1. Groups: add `mcp-readers`. Permissions: View on Dashboard, Zones, DHCP
   Server and Logs; nothing else.
2. Users: add `mcp` in `mcp-readers`.
3. Sessions: Create Token for user `mcp`, name `homelab-mcp`.
4. Zones: every zone has its own permission list, and existing zones only
   grant Administrators and DNS Administrators. Open each zone, Permissions,
   and add `mcp-readers` with View. Without this the zone list is empty and
   record reads return "Access was denied". Repeat for zones created later.

ns1 and ns2 form a Technitium cluster. Users, groups, tokens and section
permissions made on ns1 reach ns2, but zone permissions are stored per
node: repeat step 4 on ns2.

## Pi-hole (pihole-1 and pihole-2)

Pi-hole has no read-only role. Use the app password (Settings, Web
interface / API, Configure app password): with `webserver.api.app_sudo`
false, its sessions cannot change settings. The server only sends GET
requests besides `POST /api/auth`, the one allowlisted POST, and keeps one
session per node, renewed on 401. Put the app passwords in
`secrets/pihole` as `<node> <password>` lines, at mode 600. proxmox-infra
keeps the same values in HashiCorp Vault (`kv/proxmox-infra/pihole`, field
`app_passwords`), not in a file the server could share.

## Komodo

In Komodo Core as an admin:

1. Settings, Users: create a service user `mcp`.
2. User Groups: create `readers` with Read on every resource type; add
   `mcp`.
3. Back on the `mcp` user: create an API key. It shows a key and a secret;
   store them as `secrets/komodo-key` and `secrets/komodo-secret`.

## Semaphore

Login is Authentik OIDC, and its callback only works on
`https://semaphore.example.com`. Use that URL for the UI and for the token
step below, because the session cookie is marked Secure. The server itself
calls the host URL `http://10.140.0.15:3000` with the bearer token.

1. Users, New user: `mcp`, an email, a strong password. Not admin, External
   unticked.
2. Project, Team: add `mcp` with role Guest.
3. Semaphore 2.19 hides the password form, but the API login still accepts
   it. Copy the password to the clipboard and create the token through the
   API:

   ```bash
   cd "$(mktemp -d)" && pbpaste | jq -Rn '{auth:"mcp", password: input}' \
     | curl -s -c jar -H 'Content-Type: application/json' -d @- \
         https://semaphore.example.com/api/auth/login \
     && curl -s -b jar -X POST https://semaphore.example.com/api/user/tokens | jq -r .id \
       > ~/.config/homelab-mcp/secrets/semaphore \
     && chmod 600 ~/.config/homelab-mcp/secrets/semaphore \
     && rm -f jar && pbcopy < /dev/null
   ```

Semaphore 2.20 refuses password logins server-side. The token keeps
working, but creating the next one then needs an admin.

## ArgoCD and Kubernetes

These need the argo-apps change that adds the `mcp` ArgoCD account and the
`mcp-reader` ServiceAccount. After it is synced:

```bash
argocd login argo.example.com --grpc-web=false
argocd account generate-token --account mcp > ~/.config/homelab-mcp/secrets/argocd
chmod 600 ~/.config/homelab-mcp/secrets/argocd
plugins/homelab/scripts/make-readonly-kubeconfig.sh
```

The kubeconfig script prints `auth can-i` results; expect yes, yes, no,
no, no.

## Longhorn and GitLab

Longhorn's API has no authentication; the LAN boundary is the control, the
same as for its UI. GitLab uses OAuth per user: run `/mcp`, pick `gitlab`
and approve in the browser. An instance admin must first allow MCP client
access (Admin, Settings, General, Visibility and access controls).

GitLab's own server has no tool for a job's log or a pipeline's job list, so
the homelab server adds `gitlab_job_log` and `gitlab_pipeline_jobs` with a
token of its own: a group access token on `homelab`, name `mcp`, role
Reporter, scope `read_api` only (Reporter can read job logs; `read_api`
cannot write anything).

```bash
pbpaste > ~/.config/homelab-mcp/secrets/gitlab && chmod 600 ~/.config/homelab-mcp/secrets/gitlab
```

## Vault

AppRole `mcp-reader` with policy `mcp-reader`: `list` and `read` on
`kv/metadata/*` only. Metadata is path names and version history; secret
values live under `kv/data/*`, which the policy does not grant. Both are
defined in proxmox-infra `terraform/vault`. After its apply, write the
SecretID straight into the secret file and put the RoleID (an identifier,
not a credential) in `config.toml`:

```bash
vault write -f -field=secret_id auth/approle/role/mcp-reader/secret-id \
  > ~/.config/homelab-mcp/secrets/vault-secret-id
chmod 600 ~/.config/homelab-mcp/secrets/vault-secret-id
terragrunt output -raw mcp_reader_role_id   # in proxmox-infra terraform/vault
```

The seal status the overview shows needs no token.

## Grafana

The `grafana` server is the Homebrew `mcp-grafana` binary
(`brew install mcp-grafana`), started through `scripts/with-secret.sh` with
`--disable-write --disable-admin` and the incident, OnCall and Pyroscope
tools off. Its identity is the service account `mcp-reader` with the Viewer
role (Administration, Users and access, Service accounts), whose token goes
in `secrets/grafana`. Grafana keeps service accounts in its database, which
is why the argo-apps Grafana has a volume: without it a pod replacement
deletes the account.

```bash
pbpaste > ~/.config/homelab-mcp/secrets/grafana && chmod 600 ~/.config/homelab-mcp/secrets/grafana
```

## Traefik and Alertmanager

No credential. Both APIs are published on the `lan` entrypoint only and
answer without authentication (`traefik.example.com/api`,
`alertmanager.example.com/api/v2`), so the LAN boundary is the guard, as for
Longhorn. The tools issue GET requests only; Alertmanager silences are read,
never created.

## ntfy

The ntfy user `mcp` has read-only access to exactly four topics: `alerts`,
`backups`, `ci` and `deploys`. Any other topic answers 403, and so does
every publish. The user and its token live in Vault at
`kv/k8s/ntfy/ntfy-secret`: the `mcp` line in `NTFY_AUTH_USERS` and a token
with the label `mcp-reader` in `NTFY_AUTH_TOKENS`. The topic ACL is in
argo-apps `apps/user/ntfy/values.yaml`. The tool polls
(`/<topics>/json?poll=1`) with GET only, and the server keeps messages for
12 hours.

Copy the token to the clipboard, then:

```bash
pbpaste > ~/.config/homelab-mcp/secrets/ntfy && chmod 600 ~/.config/homelab-mcp/secrets/ntfy
```

To rotate it, replace the `mcp-reader` token line in `NTFY_AUTH_TOKENS` in
Vault. ntfy reads its users and tokens only at startup: force the
ExternalSecret (`kubectl -n ntfy annotate externalsecret ntfy-secret
force-sync=$(date +%s) --overwrite`) and restart the ntfy pod, then rewrite
the file the same way and run `/mcp`.

## SearXNG

No credential. The instance has no login and is published on the `lan`
entrypoint only. Its `search.formats` must include `json` (helm-charts
searxng chart value `search.formats`). If SearXNG's limiter is ever turned
on, allow the laptop in its `botdetection` `pass_ip` list, or searches answer
429.

## OpenGist

The `opengist` server writes, so its token is the one credential here that
is not read-only. In OpenGist, Settings, Access Tokens, create a token
named `mcp` with Gist "Read and write" and User "No access", and no expiry.
It can create, edit and delete your gists; the server only calls create,
get and list.

```bash
pbpaste > ~/.config/homelab-mcp/secrets/opengist && chmod 600 ~/.config/homelab-mcp/secrets/opengist
```

## Docmost

The `docmost` server writes, and Docmost Community has no API keys, so it
logs in with the email and password of a Docmost member. In Docmost,
Settings, Members, invite `claude` with role Member, copy the invite link
(no mail is configured), open it in a private window and set a password.
Leave MFA off for this account: the login endpoint returns no session
while a second factor is pending. Add the member to the `Handbook` space
with role Writer. It can edit every page in that space; the server exposes
create, update and label changes, and no delete or move.

```bash
pbpaste > ~/.config/homelab-mcp/secrets/docmost && chmod 600 ~/.config/homelab-mcp/secrets/docmost
```

## SilverBullet

The `silverbullet` server writes, so it uses its own account, not the
user's. In SilverBullet's dashboard, as the local admin: Users, Add user,
a local account `claude` that is not an admin. In the space's settings,
add `claude` as a member with role write. API tokens are issued by an
admin, not by the account itself: Admin, Users, `claude`, create a token
(it is shown once). The token can read, write and delete
files in that space; the server writes only Markdown pages, creates a page
only when it is missing, appends conditionally, and has no delete. `url`
in `[silverbullet]` is the space URL, path included.

```bash
pbpaste > ~/.config/homelab-mcp/secrets/silverbullet && chmod 600 ~/.config/homelab-mcp/secrets/silverbullet
```

## Drift doctors

`homelab_drift_report` runs the proxmox-infra doctors with these credentials
exported, so they skip their own HashiCorp Vault lookup: the Pi-hole app
passwords (`PIHOLE_CREDENTIALS`), the PBS token (`PBS_API_TOKEN_ID`,
`PBS_API_TOKEN_SECRET`) and the Semaphore token (`SEMAPHORE_API_TOKEN`).
technitium-doctor is the exception: it reads server settings, which the
zone-View token is denied, so it reads its token from Vault and needs a
`vault login`.

## Rotation

Delete and recreate the token on the target system, overwrite the file,
and run `/mcp` to reconnect. The servers resolve each secret once and keep
it until they restart, so a rotated value is used only after the reconnect.
