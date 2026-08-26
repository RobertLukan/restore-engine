#!/usr/bin/env bash
# Install Netdata on a lab Proxmox VE host (Debian/PVE).
# Run as root on each lab PVE node. Production: optional (skip if already installed).
#
# Usage:
#   sudo bash install-netdata-pve.sh
#   sudo ALLOW_FROM=192.168.123.197 bash install-netdata-pve.sh   # restrict UI/API (best-effort)
#
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root on the PVE node." >&2
  exit 1
fi

echo "==> Installing Netdata (official kickstart)"
# Non-interactive kickstart; see https://learn.netdata.cloud/docs/netdata-agent/installation
export NONINTERACTIVE=1
curl -fsSL https://get.netdata.cloud/kickstart.sh | bash -s -- --stable-channel --disable-telemetry

CONF_DIR="/etc/netdata"
if [[ ! -d "${CONF_DIR}" ]]; then
  echo "Netdata config dir not found at ${CONF_DIR}" >&2
  exit 1
fi

# Ensure Prometheus exporter is available (default on modern Netdata).
# Bind to all interfaces so Compose Prometheus on another host can scrape :19999.
echo "==> Enabling prometheus exporter / web on 0.0.0.0:19999 (adjust firewall as needed)"
mkdir -p "${CONF_DIR}"
if [[ -f "${CONF_DIR}/netdata.conf" ]]; then
  if ! grep -q '^\[web\]' "${CONF_DIR}/netdata.conf" 2>/dev/null; then
    cat >> "${CONF_DIR}/netdata.conf" <<'EOF'

[web]
    bind to = *
EOF
  fi
fi

systemctl enable --now netdata 2>/dev/null || service netdata start 2>/dev/null || true

ALLOW_FROM="${ALLOW_FROM:-}"
if [[ -n "${ALLOW_FROM}" ]] && command -v ufw >/dev/null 2>&1; then
  ufw allow from "${ALLOW_FROM}" to any port 19999 proto tcp || true
  echo "==> ufw: allowed 19999/tcp from ${ALLOW_FROM}"
elif [[ -n "${ALLOW_FROM}" ]]; then
  echo "==> Restrict :19999 to ${ALLOW_FROM} via your firewall (ufw not found)."
fi

echo "==> Checking Prometheus metrics endpoint"
sleep 2
if curl -fsS "http://127.0.0.1:19999/api/v1/allmetrics?format=prometheus" | head -n 3; then
  echo
  echo "OK. Add this host to deploy/observability/prometheus.yml netdata-pve static_configs as HOST:19999"
else
  echo "Warning: metrics endpoint not ready yet; check: systemctl status netdata" >&2
fi
