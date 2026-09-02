# avalpha on AWS (EC2 + CloudFormation)

One `t4g.small` EC2 instance runs everything: the collectors (systemd timers),
the scorer worker, and the 6am PT digest. SQLite lives on the instance's gp3
root volume — the design is single-host by construction, so one instance is the
right shape (containers/serverless would force replacing SQLite with RDS).

- **Secrets**: SSM Parameter Store (SecureString), read via the instance role.
  No keys on disk in the template or the AMI.
- **Admin**: SSM Session Manager. No inbound ports, no SSH key.
- **Cost**: roughly a few USD/month on-demand for `t4g.small` + a 20 GiB gp3
  volume; less on a 1-year Savings Plan.

## Prerequisites

- The code in a git repo the instance can clone (a public repo, or CodeCommit
  reachable via the instance role). This project isn't in git yet — `git init`,
  commit, and push somewhere first, then pass the URL as `GitRepoUrl`.
- A VPC with a subnet that has outbound internet (a public subnet, or a private
  subnet with a NAT gateway). Session Manager needs outbound 443.
- AWS CLI configured for your account.

## 1. Store the secrets in SSM

Create one SecureString parameter per secret under `/avalpha/env` (the bootstrap
strips the path prefix, so `/avalpha/env/ANTHROPIC_API_KEY` becomes
`ANTHROPIC_API_KEY` in `/etc/avalpha/env`):

```bash
for kv in \
  "ANTHROPIC_API_KEY=sk-ant-..." \
  "FINNHUB_API_KEY=..." \
  "FRED_API_KEY=..." \
  "GMAIL_APP_PASSWORD=..." \
  "REDDIT_CLIENT_ID=..." \
  "REDDIT_CLIENT_SECRET=..." \
  "AVALPHA_CONTACT_EMAIL=you@example.com" ; do
    name="${kv%%=*}"; value="${kv#*=}"
    aws ssm put-parameter --name "/avalpha/env/${name}" \
      --type SecureString --value "${value}" --overwrite
done
```

(These use the default `alias/aws/ssm` KMS key; the instance role is scoped to
decrypt only via SSM.)

## 2. Deploy the stack

```bash
aws cloudformation deploy \
  --stack-name avalpha \
  --template-file deploy/aws/avalpha.cfn.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    VpcId=vpc-xxxxxxxx \
    SubnetId=subnet-xxxxxxxx \
    GitRepoUrl=https://github.com/you/avalpha.git \
    GitBranch=main
```

Optional overrides: `InstanceType`, `AssignPublicIp` (`false` for a private
subnet + NAT), `RootVolumeSizeGb`, `SsmPathPrefix`, `EmailRecipient`,
`EmailSender`.

## 3. Verify

```bash
# Connect (SSH-free); the exact command is a stack output:
aws cloudformation describe-stacks --stack-name avalpha \
  --query "Stacks[0].Outputs" --output table

aws ssm start-session --target i-xxxxxxxx
```

In the session:

```bash
sudo cat /var/log/cloud-init-output.log | tail -40      # bootstrap result
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha list     # empty until seeded
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha add NVDA --weight 12
sudo -u avalpha /opt/avalpha/.venv/bin/avalpha status
journalctl -u avalpha-scorer -n 50
systemctl list-timers 'avalpha-*'
```

The `avalpha` CLI needs the secrets, so run it via the config the service uses:
`AVALPHA_CONFIG=/opt/avalpha/config.toml` is already the default location, and
`/etc/avalpha/env` is loaded by systemd — for hand-run CLI commands, source it
first: `set -a; . /etc/avalpha/env; set +a`.

## 4. Updating the code

Roll out a new version by connecting via Session Manager and pulling:

```bash
cd /opt/avalpha && sudo -u avalpha git pull
sudo /opt/avalpha/.venv/bin/pip install -e /opt/avalpha
# if any systemd unit changed:
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart avalpha-scorer
```

For a fully reproducible rebuild, update the git repo and re-create the stack
(the DB is on the root volume, so back it up first: copy
`/opt/avalpha/data/avalpha.db` off the instance, or move the data dir to a
separate persistent EBS volume — the recommended upgrade if you rebuild often).

## Notes

- Rotating a secret: update the SSM parameter, then on the instance re-run the
  fetch (or just reboot) and `systemctl restart avalpha-scorer`.
- The generic (non-AWS) systemd deploy is documented in
  [../../systemd/INSTALL.md](../../systemd/INSTALL.md); this template automates
  the same steps.
