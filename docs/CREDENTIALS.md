# Read-only identities

Every server carries its own credential, and that credential is read-only
on the target system. The server code is the second line of defence, not
the first. Store each secret as one file under
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
keeps the same values in its vault (`vault_pihole_app_passwords`), not in
a file the server could share.

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
calls the host URL `http://10.140.0.16:3000` with the bearer token.

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

## Rotation

Delete and recreate the token on the target system, overwrite the file,
and run `/mcp` to reconnect. Nothing else references the value.
