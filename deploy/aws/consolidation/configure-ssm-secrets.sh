#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
umask 077

write_token() {
  local parameter_name="$1"
  local output_file="$2"
  local token
  token=$(aws ssm get-parameter \
    --region "$REGION" \
    --name "$parameter_name" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text)
  printf 'TUNNEL_TOKEN=%s\n' "$token" >"$output_file"
  chown root:cloudflared "$output_file"
  chmod 0640 "$output_file"
}

install -d -m 0750 -o root -g cloudflared /etc/cloudflared
install -d -m 0750 -o avalpha -g avalpha /etc/avalpha

aws ssm get-parameters-by-path \
  --region "$REGION" \
  --path /avalpha/env \
  --recursive \
  --with-decryption \
  --query 'Parameters[].[Name,Value]' \
  --output text \
  | while IFS=$'\t' read -r name value; do
      printf '%s=%s\n' "${name##*/}" "$value"
    done >/etc/avalpha/env
chown avalpha:avalpha /etc/avalpha/env
chmod 0600 /etc/avalpha/env

write_token /valence/cf-tunnel-token /etc/cloudflared/valence.env
write_token /avalpha/env/CLOUDFLARE_TUNNEL_TOKEN /etc/cloudflared/avalpha.env
write_token /teasel/cloudflared-token /etc/cloudflared/teasel.env

echo "Parameter Store configuration written without printing secret values"
