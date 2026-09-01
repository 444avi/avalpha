# avalpha web console on Cloudflare (Tunnel + Access)

The members-only console is published through a **Cloudflare Tunnel**: the
instance runs `cloudflared`, which makes an *outbound* connection to Cloudflare
and serves `avalpha.thesilofund.com`. Nothing listens on a public port — uvicorn
is bound to `127.0.0.1:8000` and the EC2 security group has **no inbound rules**.

Access is restricted with **Cloudflare Access** (email one-time-PIN): only
allow-listed member emails can reach the app, and the app additionally verifies
the signed Access JWT (`Cf-Access-Jwt-Assertion`) as defence-in-depth.

```
member ──HTTPS──▶ Cloudflare edge ──(Access: email OTP)──▶ Tunnel ──▶ 127.0.0.1:8000 (uvicorn)
```

## Prerequisites (done / to do)

- [x] `thesilofund.com` is on Cloudflare.
- [x] A **remotely-managed tunnel** created in Zero Trust → Networks → Tunnels;
      its token is what you install `cloudflared` with. Keep the token secret —
      it authenticates the tunnel. Store it only in SSM (below), never in git.
- [ ] The tunnel's **Public Hostname** and the **Access application** (steps 1–2).

## 1. Public Hostname (tunnel ingress)

In the tunnel → **Public Hostnames** → **Add a public hostname**:

| Field | Value |
|-------|-------|
| Subdomain | `avalpha` |
| Domain | `thesilofund.com` |
| Path | *(empty — match all)* |
| Service Type | `HTTP` |
| Service URL | `localhost:8000` |

Save. Cloudflare creates the `avalpha.thesilofund.com` DNS record automatically.

## 2. Access application (members-only, email OTP)

Zero Trust → **Access** → **Applications** → **Add an application** →
**Self-hosted**:

1. **Application name:** `avalpha console`
2. **Session duration:** e.g. 24 hours.
3. **Public hostname:** `avalpha` . `thesilofund.com`.
4. **Identity providers:** enable **One-time PIN** (email OTP) — no IdP needed.
5. **Policies** → add a policy:
   - Name: `members`
   - Action: **Allow**
   - Include → **Emails** (list each member) *or* **Emails ending in**
     `@thesilofund.com`, whichever matches how members' inboxes are set up.
6. Save.

Then open the application's **Overview** and copy two values the app needs to
verify tokens:

- **Application Audience (AUD) Tag** → `CF_ACCESS_AUD`
- Your **team domain** (Zero Trust → Settings → Custom Pages / the
  `https://<team>.cloudflareaccess.com` shown in Access) → `CF_ACCESS_TEAM_DOMAIN`
  (store it **without** the `https://`, e.g. `thesilofund.cloudflareaccess.com`).

> If `CF_ACCESS_*` are set, the app strictly verifies every request's JWT and
> the local dev bypass is ignored — so a misconfigured tunnel fails closed.

## 3. Store the secrets in SSM (for the AWS deploy)

The CloudFormation instance loads every parameter under `/avalpha/env` into
`/etc/avalpha/env`, so adding these three is all that's needed:

```bash
aws ssm put-parameter --type SecureString --overwrite \
  --name /avalpha/env/CLOUDFLARE_TUNNEL_TOKEN --value 'PASTE_TUNNEL_TOKEN'

aws ssm put-parameter --type SecureString --overwrite \
  --name /avalpha/env/CF_ACCESS_TEAM_DOMAIN --value 'thesilofund.cloudflareaccess.com'

aws ssm put-parameter --type SecureString --overwrite \
  --name /avalpha/env/CF_ACCESS_AUD --value 'PASTE_AUD_TAG'
```

(Plus the app secrets already listed in [../aws/README.md](../aws/README.md):
`ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `GMAIL_APP_PASSWORD`, `REDDIT_*`,
`AVALPHA_CONTACT_EMAIL`.)

Then deploy per [../aws/README.md](../aws/README.md). On boot the instance
installs `cloudflared`, starts `avalpha-web.service` (uvicorn on loopback) and
`cloudflared.service`; the tunnel connects and `avalpha.thesilofund.com` goes
live behind Access.

## Running it locally (demo / development)

`cloudflared` is a remotely-managed tunnel, so the same token works from any
host — you can serve `avalpha.thesilofund.com` from a laptop for a demo:

```bash
# 1. the app on loopback:8000 (production auth mode — verifies Access JWTs)
export CF_ACCESS_TEAM_DOMAIN=thesilofund.cloudflareaccess.com
export CF_ACCESS_AUD=PASTE_AUD_TAG
# app secrets as usual (ANTHROPIC_API_KEY, etc.), then:
.venv/bin/uvicorn avalpha.web.app:app --host 127.0.0.1 --port 8000

# 2. the tunnel (no sudo needed; Ctrl-C to stop)
cloudflared tunnel --no-autoupdate run --token "$(cat /path/to/tunnel-token)"
```

For **local UI development without Cloudflare**, skip the tunnel and set a dev
bypass instead of the CF_ACCESS_* vars:

```bash
AVALPHA_WEB_DEV_USER=you@thesilofund.com \
  .venv/bin/uvicorn avalpha.web.app:app --port 8000
```

The dev bypass authenticates every request as that user. **Never** expose a
dev-bypass server to the internet — only run it on localhost.

To install `cloudflared` as a persistent OS service on a machine you control
(needs root), Cloudflare's own installer is:

```bash
sudo cloudflared service install <TUNNEL_TOKEN>
```

On the AWS instance this is handled by `systemd/cloudflared.service` instead.

## Security notes

- **No inbound ports.** The tunnel dials out; the SG stays inbound-deny.
- **Two independent gates.** Cloudflare Access blocks non-members at the edge;
  the app re-verifies the JWT, so it is safe even if ever reached directly.
- **Token = tunnel identity.** Anyone with the tunnel token can run *this*
  tunnel. Keep it in SSM SecureString only. To revoke, delete/rotate the tunnel
  in the Cloudflare dashboard and update the SSM parameter.
- **Least privilege for members.** Access policies decide who gets in; tighten
  the allow-list to exactly the fund's members.
