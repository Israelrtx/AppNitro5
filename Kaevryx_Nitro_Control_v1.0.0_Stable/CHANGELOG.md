# Changelog

## 1.0.0 Stable

- consolidação dos cinco perfis Acer validados no kernel 7.1.13;
- consolidação do PWM `acer-wmi` para duas ventoinhas;
- AUTO, MANUAL e TURBO;
- hotkey NitroSense baseado no mecanismo validado em v0.4.2;
- correção visual dos `QLabel` da v0.4.3 incorporada;
- daemon inicializa as ventoinhas em AUTO por segurança;
- watchdog térmico passa a ignorar sensores NVMe/SSD;
- watchdog usa CPU/Acer + NVIDIA;
- liberação da proteção térmica retorna para AUTO;
- configuração existente é preservada durante upgrade;
- remoção dos listeners/udev obsoletos dos protótipos;
- `knc-doctor` somente leitura;
- instalação com backup automático;
- CLI expandida;
- interface de fan mostra modo ativo;
- VRAM adicionada ao monitoramento;
- limpeza da árvore de arquivos para release estável.
