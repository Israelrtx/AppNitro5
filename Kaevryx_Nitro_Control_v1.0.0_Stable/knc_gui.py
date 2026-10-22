#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psutil
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from knc_client import KNCError, request
from knc_common import VERSION

SINGLE_INSTANCE = "kaevryx_nitro_control_gui_v100"
ICON_PATH = "/usr/share/icons/hicolor/scalable/apps/kaevryx-nitro-control.svg"


def fmt_temp(mC):
    return "—" if mC is None else f"{mC / 1000:.0f} °C"


def fmt_rpm(value):
    if value is None:
        return "—"
    return f"{value:,} RPM".replace(",", ".")


def nvidia_metrics():
    cmd = [
        "nvidia-smi",
        "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        values = [v.strip() for v in out.split(",")]
        return {
            "temp": int(float(values[0])),
            "util": int(float(values[1])),
            "vram_used": int(float(values[2])),
            "vram_total": int(float(values[3])),
            "power": float(values[4]),
        }
    except Exception:
        return None


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—"):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)

        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")

        self.value = QLabel(value)
        self.value.setObjectName("cardValue")

        layout.addWidget(title_label)
        layout.addWidget(self.value)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(f"Kaevryx Nitro Control {VERSION}")
        self.resize(1120, 720)
        self.setMinimumSize(900, 620)

        if Path(ICON_PATH).exists():
            self.setWindowIcon(QIcon(ICON_PATH))

        self._syncing = False
        self._last_status = None

        self.build_ui()
        self.apply_style()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)

        self.refresh()

    def build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(16)

        header = QHBoxLayout()
        titlebox = QVBoxLayout()

        title = QLabel("Kaevryx Nitro Control")
        title.setObjectName("title")
        subtitle = QLabel(f"Acer Nitro AN515-58 • Stable {VERSION}")
        subtitle.setObjectName("subtitle")

        titlebox.addWidget(title)
        titlebox.addWidget(subtitle)
        header.addLayout(titlebox)
        header.addStretch()

        header.addWidget(QLabel("Gerência de energia:"))

        self.owner = QComboBox()
        self.owner.addItem("Compatibilidade KDE (3 perfis)", "kde")
        self.owner.addItem("Controle Acer completo (5 perfis)", "acer")
        self.owner.currentIndexChanged.connect(self.owner_changed)
        header.addWidget(self.owner)

        outer.addLayout(header)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        metrics.setVerticalSpacing(12)

        self.cpu_card = MetricCard("CPU", "—")
        self.gpu_card = MetricCard("GPU", "—")
        self.fan1_card = MetricCard("Ventoinha 1", "—")
        self.fan2_card = MetricCard("Ventoinha 2", "—")
        self.profile_card = MetricCard("Perfil Acer", "—")
        self.power_card = MetricCard("GPU / VRAM", "—")

        cards = [
            self.cpu_card,
            self.gpu_card,
            self.fan1_card,
            self.fan2_card,
            self.profile_card,
            self.power_card,
        ]

        for index, card in enumerate(cards):
            metrics.addWidget(card, index // 3, index % 3)

        outer.addLayout(metrics)

        profile_box = QFrame()
        profile_box.setObjectName("panel")
        p_layout = QVBoxLayout(profile_box)

        p_layout.addWidget(self.section_label("Perfis térmicos Acer"))

        hint = QLabel(
            "Use o modo Acer completo para acessar os cinco perfis do firmware."
        )
        hint.setObjectName("hint")
        p_layout.addWidget(hint)

        profile_row = QHBoxLayout()
        self.profile_buttons = {}

        labels = [
            ("low-power", "Eco"),
            ("quiet", "Quiet"),
            ("balanced", "Balanced"),
            ("balanced-performance", "Performance"),
            ("performance", "Performance+"),
        ]

        for profile, text in labels:
            button = QPushButton(text)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, p=profile: self.select_profile(p)
            )
            profile_row.addWidget(button)
            self.profile_buttons[profile] = button

        p_layout.addLayout(profile_row)
        outer.addWidget(profile_box)

        fan_box = QFrame()
        fan_box.setObjectName("panel")
        f_layout = QVBoxLayout(fan_box)

        fan_header = QHBoxLayout()
        fan_header.addWidget(self.section_label("Controle das ventoinhas"))
        fan_header.addStretch()

        self.auto_btn = QPushButton("AUTO")
        self.manual_btn = QPushButton("MANUAL")
        self.turbo_btn = QPushButton("TURBO")

        for button in (self.auto_btn, self.manual_btn, self.turbo_btn):
            button.setCheckable(True)

        self.auto_btn.clicked.connect(lambda: self.set_fan_mode("auto"))
        self.manual_btn.clicked.connect(lambda: self.set_fan_mode("manual"))
        self.turbo_btn.clicked.connect(lambda: self.set_fan_mode("turbo"))

        fan_header.addWidget(self.auto_btn)
        fan_header.addWidget(self.manual_btn)
        fan_header.addWidget(self.turbo_btn)

        f_layout.addLayout(fan_header)

        safety = QLabel(
            "Manual: mínimo de 30%. Proteção térmica força TURBO a partir de 85 °C."
        )
        safety.setObjectName("hint")
        f_layout.addWidget(safety)

        self.fan1_slider, self.fan1_value = self.fan_slider_row(
            f_layout, "Fan 1"
        )
        self.fan2_slider, self.fan2_value = self.fan_slider_row(
            f_layout, "Fan 2"
        )

        self.fan1_slider.sliderReleased.connect(self.apply_manual)
        self.fan2_slider.sliderReleased.connect(self.apply_manual)

        outer.addWidget(fan_box)
        outer.addStretch()

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.setCentralWidget(central)

    def section_label(self, text):
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def fan_slider_row(self, parent, label_text):
        row = QHBoxLayout()

        label = QLabel(label_text)
        label.setMinimumWidth(110)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(30, 100)
        slider.setValue(50)

        value = QLabel("50%")
        value.setObjectName("sliderValue")
        value.setMinimumWidth(55)

        slider.valueChanged.connect(
            lambda v, target=value: target.setText(f"{v}%")
        )

        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(value)

        parent.addLayout(row)
        return slider, value

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #111418;
                color: #e8edf2;
                font-family: "Noto Sans", "DejaVu Sans";
                font-size: 10.5pt;
            }

            QLabel {
                border: none;
                outline: none;
                background: transparent;
                padding: 0px;
            }

            QFrame#card QLabel,
            QFrame#panel QLabel {
                border: none;
                outline: none;
                background: transparent;
            }

            QLabel#title {
                font-size: 22pt;
                font-weight: 700;
            }

            QLabel#subtitle,
            QLabel#hint {
                color: #94a0ad;
            }

            QLabel#section {
                font-size: 13pt;
                font-weight: 700;
            }

            QFrame#card,
            QFrame#panel {
                background: #181d23;
                border: 1px solid #2a323c;
                border-radius: 12px;
            }

            QLabel#cardTitle {
                color: #8e9aa7;
                font-size: 9.5pt;
            }

            QLabel#cardValue {
                font-size: 18pt;
                font-weight: 650;
                border: none;
                background: transparent;
            }

            QLabel#sliderValue {
                border: none;
                background: transparent;
                color: #e8edf2;
                padding-left: 8px;
            }

            QPushButton {
                background: #222a33;
                border: 1px solid #343f4b;
                border-radius: 8px;
                padding: 9px 14px;
            }

            QPushButton:hover {
                background: #2a3540;
            }

            QPushButton:checked {
                background: #dde5ed;
                color: #111418;
                border-color: #dde5ed;
                font-weight: 700;
            }

            QPushButton:disabled {
                color: #5b6570;
                background: #171b20;
            }

            QComboBox {
                background: #20262d;
                border: 1px solid #343f4b;
                border-radius: 7px;
                padding: 7px 10px;
                min-width: 230px;
            }

            QSlider::groove:horizontal {
                height: 5px;
                background: #303943;
                border-radius: 2px;
            }

            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: #e6edf4;
            }
        """)

    def show_error(self, message):
        self.status_bar.showMessage(message, 7000)
        QMessageBox.warning(self, "Kaevryx Nitro Control", message)

    def owner_changed(self):
        if self._syncing:
            return

        owner = self.owner.currentData()

        try:
            request({"cmd": "set_owner", "owner": owner})
            self.status_bar.showMessage(
                "Controle Acer completo ativo."
                if owner == "acer"
                else "Compatibilidade KDE ativa.",
                4000,
            )
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def select_profile(self, profile):
        try:
            if (
                self.owner.currentData() == "kde"
                and profile in ("quiet", "balanced-performance")
            ):
                answer = QMessageBox.question(
                    self,
                    "Ativar controle Acer completo?",
                    f"O perfil '{profile}' não existe na API padrão do KDE.\n\n"
                    "Deseja mudar para o controle Acer completo?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

                index = self.owner.findData("acer")
                self._syncing = True
                self.owner.setCurrentIndex(index)
                self._syncing = False

                request({"cmd": "set_owner", "owner": "acer"})

            request({"cmd": "set_profile", "profile": profile})
            self.status_bar.showMessage(
                f"Perfil aplicado: {profile}", 3000
            )
            self.refresh()

        except KNCError as exc:
            self.show_error(str(exc))

    def set_fan_mode(self, mode):
        try:
            if mode == "manual":
                self.apply_manual()
            else:
                request(
                    {"cmd": "set_fan", "fan": "both", "mode": mode}
                )

            self.status_bar.showMessage(
                f"Ventoinhas: {mode.upper()}", 3000
            )
            self.refresh()

        except KNCError as exc:
            self.show_error(str(exc))

    def apply_manual(self):
        try:
            request(
                {
                    "cmd": "set_manual_pair",
                    "fan1_percent": self.fan1_slider.value(),
                    "fan2_percent": self.fan2_slider.value(),
                }
            )
            self.status_bar.showMessage(
                "Velocidades manuais aplicadas.", 2500
            )
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def refresh(self):
        try:
            data = request({"cmd": "status"})
            self._last_status = data
            self.update_status(data)
        except KNCError as exc:
            self.status_bar.showMessage(str(exc))
            self.profile_card.value.setText("Daemon offline")

    def update_status(self, data):
        self._syncing = True
        try:
            index = self.owner.findData(
                data.get("profile_owner", "kde")
            )
            if index >= 0:
                self.owner.setCurrentIndex(index)

            profile = data.get("profile", "unknown")
            self.profile_card.value.setText(profile)

            for p, button in self.profile_buttons.items():
                button.setChecked(p == profile)

            acer = data.get("acer", {})
            self.fan1_card.value.setText(
                fmt_rpm(acer.get("fan1_input"))
            )
            self.fan2_card.value.setText(
                fmt_rpm(acer.get("fan2_input"))
            )

            cpu_util = psutil.cpu_percent(interval=None)
            cpu_temp = data.get("cpu_temp_mC")
            self.cpu_card.value.setText(
                f"{cpu_util:.0f}% • {fmt_temp(cpu_temp)}"
            )

            gpu = nvidia_metrics()
            if gpu:
                self.gpu_card.value.setText(
                    f"{gpu['util']}% • {gpu['temp']} °C"
                )
                self.power_card.value.setText(
                    f"{gpu['power']:.1f} W • "
                    f"{gpu['vram_used']}/{gpu['vram_total']} MiB"
                )
            else:
                self.gpu_card.value.setText("NVIDIA indisponível")
                self.power_card.value.setText("—")

            fan1_mode = data.get("fan1_mode", "auto")
            fan2_mode = data.get("fan2_mode", "auto")

            if fan1_mode == "manual":
                self.fan1_slider.setValue(
                    int(data.get("fan1_percent", 50))
                )
            if fan2_mode == "manual":
                self.fan2_slider.setValue(
                    int(data.get("fan2_percent", 50))
                )

            same_mode = fan1_mode if fan1_mode == fan2_mode else None
            self.auto_btn.setChecked(same_mode == "auto")
            self.manual_btn.setChecked(same_mode == "manual")
            self.turbo_btn.setChecked(same_mode == "turbo")

            enabled = data.get("fan_control_enabled", True)
            for widget in (
                self.auto_btn,
                self.manual_btn,
                self.turbo_btn,
                self.fan1_slider,
                self.fan2_slider,
            ):
                widget.setEnabled(enabled)

            if data.get("safety_override"):
                self.status_bar.showMessage(
                    data.get("last_error", "Proteção térmica ativa.")
                )
            elif data.get("last_error"):
                self.status_bar.showMessage(
                    data.get("last_error"), 5000
                )

        finally:
            self._syncing = False


def notify_running_instance() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE)

    if sock.waitForConnected(250):
        sock.write(b"SHOW\n")
        sock.flush()
        sock.waitForBytesWritten(250)
        sock.disconnectFromServer()
        return True

    return False


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Kaevryx Nitro Control")

    if notify_running_instance():
        return 0

    server = QLocalServer()
    QLocalServer.removeServer(SINGLE_INSTANCE)

    if not server.listen(SINGLE_INSTANCE):
        print(
            "Não foi possível criar servidor de instância única.",
            file=sys.stderr,
        )

    window = MainWindow()
    window.show()

    def incoming():
        connection = server.nextPendingConnection()
        if connection:
            connection.waitForReadyRead(100)
            _ = bytes(connection.readAll())
            window.showNormal()
            window.raise_()
            window.activateWindow()
            connection.disconnectFromServer()

    server.newConnection.connect(incoming)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
