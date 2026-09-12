# Arboretum EC2 consolidation runbook

Last updated: 2026-09-11 UTC

This runbook records the consolidation of Valence, AVAlpha, Sundew/Sentinel,
Overstory, and Teasel from five `us-west-1` EC2 instances onto the
CloudFormation-managed `arboretum-consolidated` host. It intentionally does
not implement a shared SEC cache or change application logic.

## Deployed infrastructure

- Stack: `arboretum-consolidated` (`UPDATE_COMPLETE`)
- Instance: `i-0549419561ebc2e27`, `t4g.medium`, Amazon Linux 2023 arm64
- Network: VPC `vpc-0e9bd8f139720dc22`, subnet
  `subnet-0f1564987ebd2477a`, one Elastic IPv4 address
  (`184.169.251.108`), and an outbound-only security group
- Administration: SSM Session Manager; there is no security-group ingress
- Storage: 16 GiB encrypted gp3 root volume plus retained, encrypted 50 GiB
  gp3 data volume `vol-050caa7907e86ae1b` mounted at `/data`
- Swap: 2 GiB `/swapfile`, persisted in `/etc/fstab`, `vm.swappiness=10`
- Backups: retained, versioned S3 bucket
  `arboretum-consolidated-backupbucket-ockhor0fneh9`
- Infrastructure source: `../consolidated.cfn.yaml`

The instance role is the union of the existing application requirements:
read access to `/valence/*`, `/avalpha/env/*`, and `/teasel/*` Parameter Store
paths; KMS decrypt through SSM; Valence's existing backup-bucket writes;
Overstory deployment-bucket reads; SES sends; writes/reads to the new backup
bucket; CloudWatch metric publication; and the SSM core managed policy.

## Application isolation and routing

| Application | Unix user | Code | Persistent data | Local origin | Public route |
| --- | --- | --- | --- | --- | --- |
| Valence | `valence` | `/opt/valence` | `/data/valence/valence.db`, `/data/valence/runs` | `127.0.0.1:8082` | `valence.arboretuminvestments.net` |
| AVAlpha | `avalpha` | `/opt/avalpha` | `/data/avalpha/avalpha.db`, `/data/avalpha/digests` | `127.0.0.1:8000` | `avalpha.arboretuminvestments.net` |
| Sundew/Sentinel | `sundew` | `/opt/sundew` | `/data/sundew/{sundew,sentinel,app}.db` | `127.0.0.1:8787` | `sundew.arboretuminvestments.net` |
| Overstory | `overstory` | `/opt/overstory` | `/data/overstory/overstory.db`, `positions.json`, `poll_log.csv` | `127.0.0.1:8081` | `overstory.arboretuminvestments.net` |
| Teasel | `teasel` | `/opt/teasel` | none | `127.0.0.1:5050` | `teasel.arboretuminvestments.net` |

Valence and Overstory's existing Cloudflare tunnel definitions both target
port 8080. Nginx listens only on `127.0.0.1:8080` and dispatches by the
Cloudflare `Host` header to 8082 or 8081. No Cloudflare route change was needed.
Each tunnel is a separate `cloudflared@APPLICATION.service` instance.

## Cutover record

Every source writer, scheduler, delivery worker, web process, and tunnel was
stopped before its target counterpart was started. This maintained exactly one
production notifier throughout the migration.

| Application | Stopped source | Preserved-state evidence |
| --- | --- | --- |
| Valence | `i-0497e0bbd6d374f8f` | `quick_check=ok`; 83 runs; 865 validations; source/target SHA-256 `0a5d4635607a5b39ce8bd0c3976793fc69944f566b98eef59886815567faf2f8` |
| AVAlpha | `i-0a66588774e91846d` | Consistent SQLite backup; `quick_check=ok`; 68 calendar events, 9,111 collector runs, 9 digests, 2 portfolios, 2 users; source/target SHA-256 `130333ca2d0a714ce1b220993a0f4f649a66e2efa269a0aa2e101f2447125a84` |
| Sundew/Sentinel | `i-0af31a49f7127703b` | Exact consistent-backup hashes: `sundew.db` `f829c09b93ddfae86bee0be6ce4a22b641e1f73ea17f4cba8939031203121190`; `sentinel.db` `d6d1b07a07af27533c60b01820a50a7819d22541bc43624956d6ee933ae313ca`; notification/deduplication `app.db` `431e1eefbb98dc1a184973d658a1e4b1fd586562a150f51dc3b04c963d033415`; all `quick_check=ok` |
| Overstory | `i-062158874d28ee934` | `quick_check=ok`; DB hash `4505bd2a33e508213d458ba11164fa6e166b24926954f9ad44ebdb9055596377`; exact hashes also matched for `positions.json` and `poll_log.csv` |
| Teasel | `i-0e9dd3b373a41b55e` | Stateless; source and target test suites passed before cutover |

The source application units were stopped and disabled so a future boot cannot
silently create a second notifier. All five source instances are now stopped,
not terminated. Their original 20/8/20/8/8 GiB volumes (64 GiB total) remain
attached and `in-use`; no volume was deleted.

## Validation evidence

- Pre-cutover target tests: Valence 54, AVAlpha 111, Sundew notification 26,
  Teasel 15; Overstory import smoke test passed.
- Target services: nginx, SSM Agent, CloudWatch Agent, all application
  services, and all five tunnel units are active.
- Target timers: all eight AVAlpha timers and `arboretum-backup.timer` are
  active.
- Local health checks passed for all five applications. Teasel returns HTTP
  200 publicly. The other four routes reach Cloudflare and return the expected
  HTTP 302 Access challenge when checked without an authenticated browser.
- AVAlpha rendered a valid PDF 1.7 document (12,589 bytes) from a disposable
  database/config copy. The validation never wrote to its production DB.
- Synthetic notification tests completed through AVAlpha, Sundew, and
  Overstory's migrated mail transports. The AVAlpha message contained only a
  synthetic PDF and no portfolio data.
- Backup `20260911T093459Z` completed. The newest S3 copy of every one of the
  six SQLite databases was restored to temporary files and returned
  `PRAGMA quick_check=ok`. State-file and manifest objects were also present.
  The initial backup set contains 179 objects totaling 59,001,093 bytes.
- CloudWatch publishes seven series: used and available memory, swap use, and
  used/free disk for `/` and `/data`. All five alarms were `OK` at cutover. At
  validation, memory use was 16.88%, available memory was 3.18 GB, and swap use
  was 0%. Alarm actions were subsequently disabled at the user's request; the
  alarms continue evaluating but do not publish `ALARM` or `OK` notifications.

## Backup and restore

`arboretum-backup.timer` runs daily at 07:15 UTC with up to five minutes of
random delay. It takes SQLite-native consistent backups, checks each copy,
syncs Valence run artifacts and AVAlpha digests, copies Overstory's state
files, and writes a SHA-256 manifest plus completion marker.

To restore, stop the affected target application units first, download the
newest object for that application, verify `sqlite3 FILE 'PRAGMA quick_check;'`
returns `ok`, preserve the current database under `.migration-snapshots`,
install the restored file with the application's ownership and mode `0640`,
and then start the application. For Sundew/Sentinel, stop and restore all of
`sundew`, `sentinel`, `delivery`, and `settings` as one group so `app.db`
remains aligned with its notification ledger.

## Rollback

Rollback must preserve the single-notifier invariant:

1. Stop the target application's tunnel first, then its writers/workers and
   timers. For Sundew/Sentinel, stop the full four-service group. For AVAlpha,
   stop all eight timers as well as the scorer and web service.
2. Take SQLite-native backups of the current target databases. They contain
   events and deduplication changes accrued after cutover and are more current
   than the frozen source copies.
3. Start only the affected old EC2 instance. Its application and tunnel units
   were disabled before shutdown and will remain inactive. Copy the current
   target state back to its original paths, verify hashes/table counts/
   `PRAGMA quick_check`, and restore ownership.
4. Re-enable and start the old application services/workers, then its old
   tunnel. Confirm
   local and public health before considering the rollback complete.
5. Leave the target units stopped until the fault is understood. Do not run
   the same tunnel/notifier on both hosts.

Original state paths are `/var/lib/valence`, `/opt/avalpha/data`,
`/home/ec2-user/sundew`, and `/opt/overstory`. Teasel is stateless. If an
application-specific rollback is unnecessary and no post-cutover state must be
kept, the frozen database on its stopped source is also the exact pre-cutover
fallback recorded above.

## Post-cutover application changes

Changes to the migrated applications made after the 2026-09-11 cutover. The
cutover record and validation evidence above are a point-in-time snapshot and
are left unedited.

### 2026-09-12 — AVAlpha swing alerter

AVAlpha gained a swing alerter: a ninth timer, `avalpha-swing.timer`, firing
every 15 minutes to email a holder when one of their holdings makes a ±10% day
move (opt-in per portfolio, default off; the market-state gate makes off-hours
firings a no-op). Like AVAlpha's other units it ships from the app's own repo
at `/opt/avalpha/systemd/` — not from this directory, which only carries the
units for apps whose source lives elsewhere — and installs with the same
`cp systemd/*.service systemd/*.timer` step. Enable it with
`systemctl enable --now avalpha-swing.timer`.

For rollback, AVAlpha now has **nine** timers, not eight: stop
`avalpha-swing.timer` together with the others in the Rollback step above.

## Cost comparison

The estimate uses 730 hours/month, Linux on-demand `us-west-1` T4g rates
(`nano` $0.005/h, `micro` $0.010/h, `small` $0.020/h, `medium` $0.040/h),
gp3 at $0.096/GB-month, and public IPv4 at $0.005/h. Taxes, data transfer,
T-family surplus CPU credits, and existing application storage are excluded.

| Recurring component | Before | After cutover |
| --- | ---: | ---: |
| EC2 compute | $47.45 | $29.20 |
| Public IPv4 | $18.25 (five) | $3.65 (one) |
| Active EBS gp3 | $6.14 (64 GB) | $6.34 (66 GB) |
| Retained old EBS required by this rollback policy | included above | $6.14 |
| Core subtotal | **$71.84** | **$45.33** |

Seven custom metric series and five standard alarms fit within AWS's first ten
free custom metrics and first ten free alarm metrics when that account-level
allowance is available. Without it, list price is about $2.10/month for the
metrics plus $0.50/month for the alarms. The initial versioned backup set is
small enough that S3 storage/request cost is negligible at current scale.

Therefore the expected bill is about **$45.33-$47.93/month while the five old
volumes are retained**, a reduction of **$23.91-$26.51/month (33%-37%)**. If
the old volumes are later deleted with explicit approval after the rollback
window, steady state becomes about **$39.19-$41.79/month**, a reduction of
**$30.05-$32.65/month (42%-45%)**.

## Deliberately deferred

- Do not terminate old instances or delete their volumes without explicit
  approval.
- Do not add a shared SEC cache without explicit approval.
- Do not change application logic as part of this infrastructure migration.

## Security follow-up

The Sundew Cloudflare connector token appeared in a diagnostic transcript
during staging. Treat it as compromised and rotate it in Cloudflare. Rotation
requires an interactive Cloudflare login and explicit confirmation because it
revokes persistent connector access. After rotation, replace
`/etc/cloudflared/sundew.env` on the target, restart
`cloudflared@sundew.service`, and revalidate the public route. Also update the
disabled source host's `/etc/cloudflared/token` before using that host for a
rollback.
