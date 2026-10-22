#!/bin/sh
# From your own machine, put Countersign on a fresh Vultr VM in one command:
#
#   VULTR_INFERENCE_API_KEY=... deploy/vultr.sh root@203.0.113.5
#
# The VM: Ubuntu 24.04, 2 vCPU / 4 GB or larger, your SSH key added when you create it.
# It installs Docker if missing, copies this checkout, writes .env on the VM (mode 600;
# the inference key lives only there, never in a sandbox), builds and starts the stack,
# and prints the public URL. HTTPS comes from Caddy on <ip>.sslip.io unless CS_SITE is set.
set -eu
host="${1:?usage: VULTR_INFERENCE_API_KEY=... deploy/vultr.sh root@<vm-ip>}"
: "${VULTR_INFERENCE_API_KEY:?set VULTR_INFERENCE_API_KEY (Vultr console > Serverless Inference)}"
ip="${host#*@}"
site="${CS_SITE:-$ip.sslip.io}"
cd "$(dirname "$0")/.."
ssh "$host" 'command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
  if command -v ufw >/dev/null 2>&1; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
  mkdir -p /opt/countersign'
COPYFILE_DISABLE=1 tar --exclude .venv --exclude .git --exclude data --exclude runs --exclude .env --exclude dist -czf - . \
  | ssh "$host" 'tar -xzf - -C /opt/countersign'
{
  echo "CS_MODEL_PROVIDER=vultr"
  echo "VULTR_INFERENCE_API_KEY=$VULTR_INFERENCE_API_KEY"
  echo "CS_MODEL=${CS_MODEL:-qwen3.8-27b}"
  echo "CS_SITE=$site"
  echo "CS_DEMO=${CS_DEMO:-1}"
  echo "CS_SPEND_CAP_USD=${CS_SPEND_CAP_USD:-20}"
  [ -n "${CS_OWNER_PASSWORD:-}" ] && echo "CS_OWNER_PASSWORD=$CS_OWNER_PASSWORD"
  true
} | ssh "$host" 'umask 077; cat > /opt/countersign/.env'
ssh "$host" 'cd /opt/countersign && sh deploy/up.sh'
echo "Countersign: https://$site"
