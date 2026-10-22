#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

VERSION="1.0.0"

say(){ printf '\n==== %s ====\n' "$*"; }
die(){ echo "ERRO: $*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -ne 0 ]] || \
  die "Execute como usuário normal, NÃO com sudo."

command -v sudo >/dev/null || die "sudo não encontrado."
sudo -v || die "Não foi possível obter autorização sudo."

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
USER_UID="$(id -u)"
USER_GID="$(id -g)"

VENDOR="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)"
MODEL="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
KERNEL="$(uname -r)"

say "Compatibilidade"
echo "Fabricante: $VENDOR"
echo "Modelo:      $MODEL"
echo "Kernel:      $KERNEL"
echo "Versão KNC:  $VERSION"

[[ "$VENDOR" == "Acer" && "$MODEL" == "Nitro AN515-58" ]] || \
  die "Esta versão estável é específica para Acer Nitro AN515-58."

[[ -e /sys/firmware/acpi/platform_profile ]] || \
  die "platform_profile não está disponível neste kernel."
[[ -e /sys/firmware/acpi/platform_profile_choices ]] || \
  die "platform_profile_choices não está disponível."

say "Dependências"
sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-pyqt6 \
  python3-psutil \
  python3-evdev \
  power-profiles-daemon \
  pciutils \
  procps

say "Parando versões anteriores"
systemctl --user disable --now kaevryx-nitro-hotkey.service 2>/dev/null || true
sudo systemctl stop kaevryx-nitro-hotkey-root.service 2>/dev/null || true
sudo systemctl stop kaevryx-nitro-control.service 2>/dev/null || true

# Remove componentes obsoletos dos protótipos.
sudo rm -f /usr/lib/systemd/user/kaevryx-nitro-hotkey.service
sudo rm -f /etc/udev/rules.d/99-kaevryx-nitro-hotkey.rules
systemctl --user daemon-reload 2>/dev/null || true

say "Backup"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ -d /opt/kaevryx-nitro-control ]]; then
  sudo cp -a \
    /opt/kaevryx-nitro-control \
    "/opt/kaevryx-nitro-control.backup-${STAMP}"
  echo "Backup: /opt/kaevryx-nitro-control.backup-${STAMP}"
fi

[[ -f /etc/kaevryx-nitro-control.json ]] && \
  sudo cp -a \
    /etc/kaevryx-nitro-control.json \
    "/etc/kaevryx-nitro-control.json.backup-${STAMP}"

say "Instalando aplicação"
sudo rm -rf /opt/kaevryx-nitro-control
sudo install -d -m 0755 /opt/kaevryx-nitro-control

for f in \
  knc_common.py \
  knc_client.py \
  knc_daemon.py \
  knc_gui.py \
  kncctl.py \
  knc_hotkey_root.py \
  knc_hotkey_bridge.py \
  knc_hotkey_test.py \
  knc_doctor.py
do
  sudo install -m 0755 \
    "$ROOT/$f" \
    "/opt/kaevryx-nitro-control/$f"
done

say "Configuração"
TMP_CONFIG="$(mktemp)"
python3 - "$USER_UID" "$USER_GID" >"$TMP_CONFIG" <<'PY'
import json
import os
import sys

uid = int(sys.argv[1])
gid = int(sys.argv[2])
path = "/etc/kaevryx-nitro-control.json"

defaults = {
    "allowed_uid": uid,
    "allowed_gid": gid,
    "fan_min_percent": 30,
    "fan_min_pwm": 77,
    "fan_max_pwm": 255,
    "safety_temp_mC": 85000,
    "safety_release_temp_mC": 78000,
    "critical_temp_mC": 93000,
    "fan_control_enabled": True,
    "force_fans_auto_on_daemon_start": True,
}

current = {}
try:
    with open(path, "r", encoding="utf-8") as f:
        value = json.load(f)
        if isinstance(value, dict):
            current = value
except Exception:
    pass

merged = {**defaults, **current}
merged["allowed_uid"] = uid
merged["allowed_gid"] = gid
print(json.dumps(merged, indent=2, sort_keys=True))
PY

sudo install -m 0644 "$TMP_CONFIG" /etc/kaevryx-nitro-control.json
rm -f "$TMP_CONFIG"

say "Comandos"
sudo tee /usr/local/bin/kaevryx-nitro-control >/dev/null <<'EOF'
#!/usr/bin/env bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
exec /usr/bin/python3 /opt/kaevryx-nitro-control/knc_gui.py "$@"
EOF

sudo tee /usr/local/bin/kncctl >/dev/null <<'EOF'
#!/usr/bin/env bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
exec /usr/bin/python3 /opt/kaevryx-nitro-control/kncctl.py "$@"
EOF

sudo tee /usr/local/bin/knc-doctor >/dev/null <<'EOF'
#!/usr/bin/env bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
exec /usr/bin/python3 /opt/kaevryx-nitro-control/knc_doctor.py "$@"
EOF

sudo tee /usr/local/bin/knc-hotkey-test >/dev/null <<'EOF'
#!/usr/bin/env bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
exec /usr/bin/python3 /opt/kaevryx-nitro-control/knc_hotkey_test.py "$@"
EOF

sudo chmod 0755 \
  /usr/local/bin/kaevryx-nitro-control \
  /usr/local/bin/kncctl \
  /usr/local/bin/knc-doctor \
  /usr/local/bin/knc-hotkey-test

say "Serviços"
sudo install -m 0644 \
  "$ROOT/kaevryx-nitro-control.service" \
  /etc/systemd/system/kaevryx-nitro-control.service

sudo install -m 0644 \
  "$ROOT/kaevryx-nitro-hotkey-root.service" \
  /etc/systemd/system/kaevryx-nitro-hotkey-root.service

sudo systemctl daemon-reload
sudo systemctl enable --now kaevryx-nitro-control.service
sudo systemctl enable --now kaevryx-nitro-hotkey-root.service

say "Integração gráfica"
sudo install -m 0644 \
  "$ROOT/kaevryx-nitro-control.desktop" \
  /usr/share/applications/kaevryx-nitro-control.desktop

sudo install -D -m 0644 \
  "$ROOT/kaevryx-nitro-control.svg" \
  /usr/share/icons/hicolor/scalable/apps/kaevryx-nitro-control.svg

mkdir -p "$HOME/.config/autostart" "$HOME/.cache"

install -m 0644 \
  "$ROOT/kaevryx-nitro-hotkey-bridge.desktop" \
  "$HOME/.config/autostart/kaevryx-nitro-hotkey-bridge.desktop"

say "Bridge NitroSense"
pkill -u "$USER_UID" \
  -f '/opt/kaevryx-nitro-control/knc_hotkey_bridge.py' \
  2>/dev/null || true

rm -f \
  "${XDG_RUNTIME_DIR:-/run/user/$USER_UID}/kaevryx-nitro-hotkey.sock" \
  2>/dev/null || true

nohup /usr/bin/python3 \
  /opt/kaevryx-nitro-control/knc_hotkey_bridge.py \
  >"$HOME/.cache/kaevryx-nitro-hotkey-bridge.log" \
  2>&1 &

sleep 1

say "Autotestes"
kncctl ping
kncctl status >/tmp/knc-stable-status.json
cat /tmp/knc-stable-status.json

SOCK="${XDG_RUNTIME_DIR:-/run/user/$USER_UID}/kaevryx-nitro-hotkey.sock"
[[ -S "$SOCK" ]] || {
  cat "$HOME/.cache/kaevryx-nitro-hotkey-bridge.log" 2>/dev/null || true
  die "Bridge da tecla Nitro não iniciou: $SOCK"
}

sudo systemctl is-active --quiet kaevryx-nitro-control.service || \
  die "Daemon KNC não está ativo."

sudo systemctl is-active --quiet kaevryx-nitro-hotkey-root.service || \
  die "Listener Nitro não está ativo."

say "Instalação concluída"
echo "Kaevryx Nitro Control $VERSION Stable instalado."
echo
echo "Abrir:"
echo "  kaevryx-nitro-control"
echo
echo "Diagnóstico:"
echo "  knc-doctor"
echo
echo "CLI:"
echo "  kncctl status"
echo
echo "Teste do bridge Nitro:"
echo "  knc-hotkey-test"
