# Kaevryx Nitro Control 1.0.0 Stable

Controle nativo para o **Acer Nitro AN515-58** no Debian 13/KDE.

Esta versão consolida os componentes validados durante o desenvolvimento:
perfis Acer, PWM das duas ventoinhas, NVIDIA proprietária, integração KDE e
tecla física NitroSense.

## Recursos

- 5 perfis Acer:
  - `low-power`
  - `quiet`
  - `balanced`
  - `balanced-performance`
  - `performance`
- dois modos de gerência:
  - Compatibilidade KDE / `power-profiles-daemon`
  - Controle Acer completo
- ventoinhas:
  - AUTO
  - MANUAL 30–100%
  - TURBO
- watchdog térmico;
- retorno automático para AUTO após evento térmico;
- monitoramento CPU, GPU, VRAM, potência e RPM;
- NVIDIA via `nvidia-smi`;
- tecla física NitroSense;
- daemon privilegiado separado da GUI;
- socket Unix validado por UID;
- interface PyQt6;
- `kncctl` para CLI;
- `knc-doctor` para diagnóstico somente leitura.

## Instalação

```bash
chmod +x install.sh
./install.sh
```

Não execute o instalador inteiro com `sudo`.

Depois:

```bash
kaevryx-nitro-control
```

## Comandos úteis

```bash
kncctl status
kncctl owner acer
kncctl profile quiet
kncctl profile balanced-performance
kncctl fan both auto
kncctl fan both turbo
kncctl fan 1 manual 55
kncctl fan 2 manual 60
kncctl reset-fans
```

Diagnóstico:

```bash
knc-doctor
```

Hotkey:

```bash
knc-hotkey-test
sudo journalctl -fu kaevryx-nitro-hotkey-root.service
```

## Segurança

- nenhum endereço EC arbitrário é escrito;
- controle de fan usa somente `acer-wmi`/hwmon exposto pelo kernel;
- manual tem piso de 30%;
- ao parar o daemon, ventoinhas retornam para AUTO;
- watchdog usa sensores CPU/Acer e temperatura NVIDIA;
- SSD/NVMe não é usado para disparar o watchdog das ventoinhas;
- após um evento térmico, o sistema retorna para AUTO em vez de restaurar
  automaticamente uma velocidade manual anterior.

## Arquitetura

```text
GUI PyQt6
   │
   │ Unix socket
   ▼
KNC daemon (root)
   ├── platform_profile
   ├── power-profiles-daemon
   ├── acer-wmi hwmon / PWM
   └── watchdog térmico

Nitro key (/dev/input)
   │
   ▼
listener root
   │ socket /run/user/<uid>
   ▼
bridge Plasma/Wayland
   │
   ▼
GUI / instância existente
```
