#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[[ ${EUID:-$(id -u)} -ne 0 ]] || {
  echo "Execute como usuário normal, NÃO com sudo."
  exit 1
}

sudo -v

# Best-effort safe state.
kncctl fan both auto 2>/dev/null || true
kncctl owner kde 2>/dev/null || true

pkill -u "$(id -u)" \
  -f '/opt/kaevryx-nitro-control/knc_hotkey_bridge.py' \
  2>/dev/null || true

sudo systemctl disable --now \
  kaevryx-nitro-hotkey-root.service \
  2>/dev/null || true

sudo systemctl disable --now \
  kaevryx-nitro-control.service \
  2>/dev/null || true

sudo rm -f \
  /etc/systemd/system/kaevryx-nitro-control.service \
  /etc/systemd/system/kaevryx-nitro-hotkey-root.service \
  /usr/local/bin/kaevryx-nitro-control \
  /usr/local/bin/kncctl \
  /usr/local/bin/knc-doctor \
  /usr/local/bin/knc-hotkey-test \
  /usr/share/applications/kaevryx-nitro-control.desktop \
  /usr/share/icons/hicolor/scalable/apps/kaevryx-nitro-control.svg

rm -f "$HOME/.config/autostart/kaevryx-nitro-hotkey-bridge.desktop"
rm -f "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/kaevryx-nitro-hotkey.sock"

sudo rm -rf /opt/kaevryx-nitro-control

sudo systemctl daemon-reload
sudo systemctl start power-profiles-daemon.service 2>/dev/null || true

echo "Kaevryx Nitro Control removido."
echo "A configuração /etc/kaevryx-nitro-control.json foi preservada."
