#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

say(){ printf '\n==== %s ====\n' "$*"; }
die(){ echo "ERRO: $*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -ne 0 ]] || \
  die "Execute como usuário normal, NÃO com sudo."

sudo -v || die "sudo indisponível."

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/opt/kaevryx-nitro-control/knc_gui.py"

[[ -f "$TARGET" ]] || \
  die "KNC não encontrado em $TARGET"

say "Backup da GUI atual"
STAMP="$(date +%Y%m%d-%H%M%S)"
sudo cp -a \
  "$TARGET" \
  "${TARGET}.backup-ui-${STAMP}"

say "Aplicando UI Uniformity v1.6.2"
sudo install -m 0755 \
  "$ROOT/knc_gui.py" \
  "$TARGET"

say "Validando sintaxe"
sudo /usr/bin/python3 -m py_compile \
  "$TARGET"

say "Reiniciando somente a GUI"
pkill -u "$(id -u)" \
  -f '/opt/kaevryx-nitro-control/knc_gui.py' \
  2>/dev/null || true

sleep 1

nohup kaevryx-nitro-control --tray \
  >"$HOME/.cache/kaevryx-nitro-control.log" \
  2>&1 &

sleep 2

echo
echo "UI Uniformity v1.6.2 aplicada."
echo "Abra pelo tray, tecla Nitro ou:"
echo "  kaevryx-nitro-control"
