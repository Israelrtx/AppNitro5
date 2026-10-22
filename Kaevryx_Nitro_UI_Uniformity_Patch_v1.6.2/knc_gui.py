#!/usr/bin/env python3
from __future__ import annotations

import math
import json
import subprocess
import sys
from collections import deque
from pathlib import Path

import psutil
from PyQt6.QtCore import QPointF, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
    QSystemTrayIcon,
    QMenu,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QSpinBox,
    QScrollArea,
    QColorDialog,
)

from knc_client import KNCError, request
from knc_common import VERSION
from knc_rgb_backend import (
    RGBBackendError,
    apply_lighting,
    power_lighting,
    probe_rgb_backend,
)

SINGLE_INSTANCE = "kaevryx_nitro_control_gui_v162"
ICON_PATH = "/usr/share/icons/hicolor/scalable/apps/kaevryx-nitro-control.svg"


def fmt_temp(mC):
    return "—" if mC is None else f"{mC / 1000:.0f}°C"


def fmt_rpm(value):
    if value is None:
        return "—"
    return f"{value:,}".replace(",", ".")


def nvidia_metrics():
    cmd = [
        "nvidia-smi",
        "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,clocks.gr,clocks.mem",
        "--format=csv,noheader,nounits",
    ]

    try:
        out = subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()

        values = [
            value.strip()
            for value in out.split(",")
        ]

        return {
            "temp": int(float(values[0])),
            "util": int(float(values[1])),
            "vram_used": int(float(values[2])),
            "vram_total": int(float(values[3])),
            "power": float(values[4]),
            "clock_gpu": int(float(values[5])),
            "clock_mem": int(float(values[6])),
        }

    except Exception:
        return None


class SensorCard(QFrame):
    def __init__(self, title, accent="#e21d2f"):
        super().__init__()
        self.setObjectName("sensorCard")
        self.accent = accent
        self.setProperty("alert", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            16,
            13,
            16,
            13,
        )

        self.title = QLabel(title)
        self.title.setObjectName("sensorTitle")

        self.value = QLabel("—")
        self.value.setObjectName("sensorValue")

        self.detail = QLabel("")
        self.detail.setObjectName("sensorDetail")

        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set_alert(self, active):
        active = bool(active)

        if bool(
            self.property("alert")
        ) == active:
            return

        self.setProperty(
            "alert",
            active,
        )

        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class HistoryChart(QWidget):
    """Temperature chart backed by timestamped persistent telemetry."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(260)
        self.samples = []
        self.range_minutes = 15
        self.setToolTip(
            "Histórico persistente de temperatura CPU/GPU"
        )

    def set_range_minutes(self, minutes):
        self.range_minutes = max(
            1,
            min(60, int(minutes)),
        )
        self.update()

    def set_samples(self, samples):
        cleaned = []

        for item in samples or []:
            if not isinstance(item, dict):
                continue

            try:
                ts = int(item.get("ts", 0))
            except Exception:
                continue

            if ts <= 0:
                continue

            cpu_mC = item.get("cpu_temp_mC")
            gpu_mC = item.get("gpu_temp_mC")

            def to_c(value):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return None

                value = value / 1000.0

                # Invalid / absent sensor samples are gaps, never 0 °C.
                if value < 0 or value > 130:
                    return None

                return value

            cleaned.append({
                "ts": ts,
                "cpu": to_c(cpu_mC),
                "gpu": to_c(gpu_mC),
            })

        cleaned.sort(key=lambda row: row["ts"])

        # Deduplicate identical timestamps, retaining the latest value.
        dedup = {}
        for row in cleaned:
            dedup[row["ts"]] = row

        self.samples = [
            dedup[key]
            for key in sorted(dedup)
        ]
        self.update()

    def plot_rect(self):
        return self.rect().adjusted(
            55,
            20,
            -20,
            -44,
        )

    def visible_samples(self):
        if not self.samples:
            return []

        latest = self.samples[-1]["ts"]
        cutoff = latest - self.range_minutes * 60

        return [
            row
            for row in self.samples
            if row["ts"] >= cutoff
        ]

    def temperature_bounds(self, rows):
        values = []

        for row in rows:
            for key in ("cpu", "gpu"):
                value = row.get(key)
                if value is not None:
                    values.append(value)

        if not values:
            return 20.0, 100.0

        low = min(values)
        high = max(values)

        # Stable bounds: avoid graph constantly changing scale.
        ymin = max(
            0.0,
            min(30.0, math.floor((low - 8.0) / 10.0) * 10.0),
        )
        ymax = min(
            120.0,
            max(80.0, math.ceil((high + 8.0) / 10.0) * 10.0),
        )

        if ymax - ymin < 40:
            ymax = min(120.0, ymin + 40.0)

        return ymin, ymax

    def sample_point(
        self,
        rect,
        row,
        key,
        first_ts,
        last_ts,
        ymin,
        ymax,
    ):
        value = row.get(key)
        if value is None:
            return None

        total = max(1, last_ts - first_ts)
        x = (
            rect.left()
            + (row["ts"] - first_ts)
            / total
            * rect.width()
        )

        span = max(1.0, ymax - ymin)
        y = (
            rect.bottom()
            - (value - ymin)
            / span
            * rect.height()
        )

        return QPointF(x, y)

    def draw_series(
        self,
        painter,
        rect,
        rows,
        key,
        color,
        first_ts,
        last_ts,
        ymin,
        ymax,
    ):
        painter.setPen(
            QPen(
                QColor(color),
                2.2,
            )
        )

        # Build separate paths across missing samples so sensor gaps
        # do not create artificial dives to 0 °C.
        path = QPainterPath()
        drawing = False

        for row in rows:
            point = self.sample_point(
                rect,
                row,
                key,
                first_ts,
                last_ts,
                ymin,
                ymax,
            )

            if point is None:
                if drawing:
                    painter.drawPath(path)
                    path = QPainterPath()
                    drawing = False
                continue

            if not drawing:
                path.moveTo(point)
                drawing = True
            else:
                path.lineTo(point)

        if drawing:
            painter.drawPath(path)

    def format_clock(self, timestamp):
        try:
            import datetime
            return datetime.datetime.fromtimestamp(
                timestamp
            ).strftime("%H:%M")
        except Exception:
            return "--:--"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing
        )

        painter.fillRect(
            self.rect(),
            QColor("#111316"),
        )

        rect = self.plot_rect()
        rows = self.visible_samples()

        painter.setPen(
            QPen(
                QColor("#2c3035"),
                1,
            )
        )

        ymin, ymax = self.temperature_bounds(rows)

        # Horizontal temperature grid.
        grid_count = 5
        for index in range(grid_count + 1):
            ratio = index / grid_count
            y = rect.bottom() - ratio * rect.height()

            painter.setPen(
                QPen(QColor("#2c3035"), 1)
            )
            painter.drawLine(
                int(rect.left()),
                int(y),
                int(rect.right()),
                int(y),
            )

            temp = ymin + ratio * (ymax - ymin)
            painter.setPen(QColor("#747c84"))
            painter.drawText(
                8,
                int(y + 4),
                f"{temp:.0f}°",
            )

        # Empty-state is explicit instead of drawing bogus 0 °C data.
        if not rows:
            painter.setPen(QColor("#747c84"))
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                "Aguardando telemetria...",
            )
            return

        first_ts = rows[0]["ts"]
        last_ts = rows[-1]["ts"]

        if last_ts <= first_ts:
            last_ts = first_ts + 1

        # Time grid uses the actual telemetry timestamps rather than
        # treating irregular samples as equally spaced categories.
        time_ticks = 4
        for index in range(time_ticks + 1):
            ratio = index / time_ticks
            x = rect.left() + ratio * rect.width()
            timestamp = round(
                first_ts
                + ratio * (last_ts - first_ts)
            )

            painter.setPen(
                QPen(QColor("#24282d"), 1)
            )
            painter.drawLine(
                int(x),
                int(rect.top()),
                int(x),
                int(rect.bottom()),
            )

            painter.setPen(QColor("#697179"))
            label = self.format_clock(timestamp)
            painter.drawText(
                int(x - 18),
                int(rect.bottom() + 24),
                label,
            )

        self.draw_series(
            painter,
            rect,
            rows,
            "cpu",
            "#e21d2f",
            first_ts,
            last_ts,
            ymin,
            ymax,
        )
        self.draw_series(
            painter,
            rect,
            rows,
            "gpu",
            "#39a9ff",
            first_ts,
            last_ts,
            ymin,
            ymax,
        )

        # Legend.
        painter.setPen(QColor("#e21d2f"))
        painter.drawText(
            int(rect.left()),
            14,
            "CPU",
        )

        painter.setPen(QColor("#39a9ff"))
        painter.drawText(
            int(rect.left()) + 55,
            14,
            "GPU",
        )

        painter.setPen(QColor("#8a929a"))
        painter.drawText(
            int(rect.right()) - 120,
            14,
            f"{self.range_minutes} min",
        )


class FanDial(QWidget):
    def __init__(self, label):
        super().__init__()
        self.label = label
        self.rpm = 0
        self.percent = 0
        self.angle = 0.0
        self.setMinimumSize(155, 155)
        self.setMaximumSize(180, 180)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate)
        self.timer.start(50)

    def set_data(self, rpm, percent):
        self.rpm = int(rpm or 0)
        self.percent = int(percent or 0)
        self.update()

    def animate(self):
        speed = 1.0 + min(12.0, self.rpm / 450.0)
        self.angle = (self.angle + speed) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height()) - 18
        rect = QRectF(
            (self.width() - side) / 2,
            (self.height() - side) / 2,
            side,
            side,
        )
        center = rect.center()
        radius = rect.width() / 2

        painter.setPen(QPen(QColor("#3b4046"), 2))
        painter.setBrush(QColor("#0c0e10"))
        painter.drawEllipse(rect)

        painter.setPen(QPen(QColor("#e21d2f"), 4))
        arc = QRectF(
            rect.left() + 5,
            rect.top() + 5,
            rect.width() - 10,
            rect.height() - 10,
        )
        painter.drawArc(
            arc,
            90 * 16,
            -int(360 * 16 * max(0, min(100, self.percent)) / 100),
        )

        painter.save()
        painter.translate(center)
        painter.rotate(self.angle)

        blade_color = QColor("#565d64")
        for i in range(7):
            painter.save()
            painter.rotate(i * (360 / 7))
            path = QPainterPath()
            path.moveTo(8, -7)
            path.cubicTo(
                radius * 0.26, -radius * 0.44,
                radius * 0.64, -radius * 0.34,
                radius * 0.72, -8,
            )
            path.cubicTo(
                radius * 0.50, 6,
                radius * 0.22, 15,
                8, 7,
            )
            path.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(blade_color)
            painter.drawPath(path)
            painter.restore()

        painter.setBrush(QColor("#15181b"))
        painter.setPen(QPen(QColor("#6a7179"), 2))
        painter.drawEllipse(QPointF(0, 0), radius * 0.20, radius * 0.20)
        painter.restore()

        painter.setPen(QColor("#f4f5f6"))
        font = QFont(painter.font())
        font.setBold(True)
        font.setPointSize(16)
        painter.setFont(font)
        painter.drawText(
            QRectF(rect.left(), center.y() - 18, rect.width(), 35),
            Qt.AlignmentFlag.AlignCenter,
            f"{self.rpm}",
        )

        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#9da4ab"))
        painter.drawText(
            QRectF(rect.left(), center.y() + 14, rect.width(), 22),
            Qt.AlignmentFlag.AlignCenter,
            "RPM",
        )
        painter.setPen(QColor("#e21d2f"))
        painter.drawText(
            QRectF(rect.left(), rect.bottom() - 30, rect.width(), 22),
            Qt.AlignmentFlag.AlignCenter,
            self.label,
        )


class DualCurveEditor(QWidget):
    curveChanged = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(255)
        self.cpu_points = [
            [45, 30], [55, 40], [65, 52], [72, 65],
            [78, 78], [84, 92], [90, 100],
        ]
        self.gpu_points = [p[:] for p in self.cpu_points]
        self.active_series = "cpu"
        self.drag_index = None
        self.setMouseTracking(True)

    def set_curves(self, cpu, gpu):
        if cpu:
            self.cpu_points = [list(map(int, p)) for p in cpu]
        if gpu:
            self.gpu_points = [list(map(int, p)) for p in gpu]
        self.update()

    def curves(self):
        return (
            [p[:] for p in self.cpu_points],
            [p[:] for p in self.gpu_points],
        )

    def set_active_series(self, series):
        self.active_series = "gpu" if series == "gpu" else "cpu"
        self.drag_index = None
        self.update()

    def plot_rect(self):
        return self.rect().adjusted(58, 28, -28, -48)

    def point_to_screen(self, point):
        rect = self.plot_rect()
        temp, percent = point
        x = rect.left() + (temp - 35) / 65 * rect.width()
        y = rect.bottom() - (percent - 30) / 70 * rect.height()
        return QPointF(x, y)

    def screen_to_point(self, pos, index, series):
        rect = self.plot_rect()
        x = max(rect.left(), min(rect.right(), pos.x()))
        y = max(rect.top(), min(rect.bottom(), pos.y()))

        temp = round(35 + (x - rect.left()) / rect.width() * 65)
        percent = round(30 + (rect.bottom() - y) / rect.height() * 70)

        points = self.cpu_points if series == "cpu" else self.gpu_points
        if index > 0:
            temp = max(temp, points[index - 1][0] + 2)
        if index < len(points) - 1:
            temp = min(temp, points[index + 1][0] - 2)

        return [max(35, min(100, temp)), max(30, min(100, percent))]

    def nearest_index(self, pos, series):
        points = self.cpu_points if series == "cpu" else self.gpu_points
        nearest = None
        best = 18.0
        for i, p in enumerate(points):
            sp = self.point_to_screen(p)
            dist = math.hypot(sp.x() - pos.x(), sp.y() - pos.y())
            if dist < best:
                best = dist
                nearest = i
        return nearest

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_index = self.nearest_index(
                event.position(), self.active_series
            )
            if self.drag_index is not None:
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.drag_index is None:
            idx = self.nearest_index(event.position(), self.active_series)
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if idx is not None
                else Qt.CursorShape.ArrowCursor
            )
            return

        points = (
            self.cpu_points
            if self.active_series == "cpu"
            else self.gpu_points
        )
        points[self.drag_index] = self.screen_to_point(
            event.position(),
            self.drag_index,
            self.active_series,
        )
        self.curveChanged.emit()
        self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_index is not None:
            self.drag_index = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _draw_curve(self, painter, points, color, active):
        if not points:
            return

        painter.save()
        painter.setClipRect(
            self.plot_rect()
        )

        pen = QPen(
            QColor(color),
            3 if active else 2,
        )
        painter.setPen(pen)

        # IMPORTANT: never fill the QPainterPath. The previous code left
        # the point brush active between series and Qt filled the path,
        # producing the large white triangular artifact.
        painter.setBrush(
            Qt.BrushStyle.NoBrush
        )

        path = QPainterPath()

        for index, point in enumerate(points):
            screen_point = self.point_to_screen(
                point
            )

            if index == 0:
                path.moveTo(
                    screen_point
                )
            else:
                path.lineTo(
                    screen_point
                )

        painter.drawPath(
            path
        )

        for point in points:
            screen_point = self.point_to_screen(
                point
            )

            painter.setPen(
                QPen(
                    QColor(color),
                    2,
                )
            )
            painter.setBrush(
                QColor("#f4f5f6")
                if active
                else QColor(color)
            )
            painter.drawEllipse(
                screen_point,
                5 if active else 4,
                5 if active else 4,
            )

        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#101215"))

        rect = self.plot_rect()

        painter.setPen(QPen(QColor("#2c3035"), 1))
        for temp in range(40, 101, 10):
            x = rect.left() + (temp - 35) / 65 * rect.width()
            painter.drawLine(
                int(x), int(rect.top()), int(x), int(rect.bottom())
            )
            painter.setPen(QColor("#6f777f"))
            painter.drawText(
                int(x - 12), int(rect.bottom() + 22), f"{temp}°"
            )
            painter.setPen(QPen(QColor("#2c3035"), 1))

        for percent in range(30, 101, 10):
            y = rect.bottom() - (percent - 30) / 70 * rect.height()
            painter.drawLine(
                int(rect.left()), int(y), int(rect.right()), int(y)
            )
            painter.setPen(QColor("#6f777f"))
            painter.drawText(
                12, int(y + 4), f"{percent}%"
            )
            painter.setPen(QPen(QColor("#2c3035"), 1))

        self._draw_curve(
            painter,
            self.cpu_points,
            "#e21d2f",
            self.active_series == "cpu",
        )
        self._draw_curve(
            painter,
            self.gpu_points,
            "#39a9ff",
            self.active_series == "gpu",
        )

        painter.setPen(QColor("#8c949c"))
        painter.drawText(
            QRectF(rect.left(), 4, rect.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            "Arraste os pontos para editar a curva temperatura × fan",
        )

class ZonePreview(QWidget):
    """Four-zone Nitro keyboard preview.

    RGB animation is visual unless a future hardware backend is present.
    Brightness uses the existing safe keyboard backlight backend.
    """

    def __init__(self):
        super().__init__()

        self.setMinimumHeight(165)
        self.setMaximumHeight(190)

        self.colors = [
            QColor("#ff0000"),
            QColor("#ff0000"),
            QColor("#ff0000"),
            QColor("#ff0000"),
        ]

        self.effect = "static"
        self.speed = 50
        self.phase = 0.0
        self.cpu_temp = None
        self.gpu_temp = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(
            self.animate
        )
        self.timer.start(40)

    def set_colors(self, colors):
        values = []

        for value in colors[:4]:
            color = QColor(value)
            values.append(
                color
                if color.isValid()
                else QColor("#ff0000")
            )

        while len(values) < 4:
            values.append(QColor("#ff0000"))

        self.colors = values
        self.update()

    def set_effect(self, effect):
        self.effect = str(effect or "static")
        self.update()

    def set_speed(self, speed):
        self.speed = max(
            0,
            min(100, int(speed)),
        )

    def set_temperatures(
        self,
        cpu_temp,
        gpu_temp,
    ):
        self.cpu_temp = cpu_temp
        self.gpu_temp = gpu_temp
        self.update()

    def animate(self):
        if self.effect not in (
            "breathing",
            "wave",
        ):
            return

        delta = 0.015 + (
            self.speed / 100.0
        ) * 0.08

        self.phase = (
            self.phase + delta
        ) % 1.0
        self.update()

    def temperature_color(self, value):
        if value is None:
            return QColor("#50555a")

        value = max(
            35.0,
            min(95.0, float(value)),
        )

        ratio = (
            value - 35.0
        ) / 60.0

        # Blue -> orange -> red temperature visualization.
        if ratio < 0.5:
            local = ratio / 0.5
            r = int(30 + 225 * local)
            g = int(140 + 80 * local)
            b = int(255 - 190 * local)
        else:
            local = (
                ratio - 0.5
            ) / 0.5
            r = 255
            g = int(220 - 190 * local)
            b = int(65 - 55 * local)

        return QColor(r, g, b)

    def effective_color(self, index):
        if self.effect == "temperature":
            # Left half follows CPU, right half GPU.
            temp = (
                self.cpu_temp
                if index < 2
                else self.gpu_temp
            )
            return self.temperature_color(temp)

        base = QColor(self.colors[index])

        if self.effect == "breathing":
            factor = (
                0.28
                + 0.72
                * (
                    0.5
                    + 0.5
                    * math.sin(
                        self.phase
                        * math.tau
                    )
                )
            )
            return QColor(
                int(base.red() * factor),
                int(base.green() * factor),
                int(base.blue() * factor),
            )

        if self.effect == "wave":
            factor = (
                0.24
                + 0.76
                * (
                    0.5
                    + 0.5
                    * math.sin(
                        (
                            self.phase
                            + index * 0.18
                        )
                        * math.tau
                    )
                )
            )
            return QColor(
                int(base.red() * factor),
                int(base.green() * factor),
                int(base.blue() * factor),
            )

        return base

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing
        )

        painter.fillRect(
            self.rect(),
            QColor("#111316"),
        )

        base = self.rect().adjusted(
            26,
            30,
            -26,
            -18,
        )

        painter.setPen(
            QPen(
                QColor("#454b52"),
                2,
            )
        )
        painter.setBrush(
            QColor("#0b0d0f")
        )
        painter.drawRoundedRect(
            base,
            14,
            14,
        )

        width = base.width() / 4

        for index in range(4):
            color = self.effective_color(
                index
            )

            zone = QRectF(
                base.left()
                + index * width
                + 8,
                base.top() + 13,
                width - 16,
                base.height() - 26,
            )

            glow = QColor(color)
            glow.setAlpha(105)

            painter.setPen(
                QPen(
                    color,
                    2,
                )
            )
            painter.setBrush(
                glow
            )
            painter.drawRoundedRect(
                zone,
                7,
                7,
            )

            painter.setPen(
                QColor("#eef1f4")
            )
            painter.drawText(
                zone,
                Qt.AlignmentFlag.AlignCenter,
                f"ZONE {index + 1}",
            )

        painter.setPen(
            QColor("#7d858d")
        )
        painter.drawText(
            QRectF(
                base.left(),
                5,
                base.width(),
                20,
            ),
            Qt.AlignmentFlag.AlignCenter,
            "4-ZONE KEYBOARD PREVIEW",
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Kaevryx Nitro Control {VERSION}")
        self.resize(1240, 790)
        self.setMinimumSize(1040, 680)
        if Path(ICON_PATH).exists():
            self.setWindowIcon(QIcon(ICON_PATH))

        self._syncing = False
        self._last_status = {}
        self._osd_last_profile = None
        self._osd_last_boost = False
        self.history_cpu = deque(maxlen=720)
        self.history_gpu = deque(maxlen=720)

        self.user_config_dir = (
            Path.home() / ".config" / "kaevryx-nitro-control"
        )
        self.user_config_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_file = self.user_config_dir / "profiles.json"
        self.ui_settings_file = (
            self.user_config_dir
            / "ui-settings.json"
        )
        self.lighting_file = (
            self.user_config_dir
            / "lighting.json"
        )

        self.user_profiles = self.load_user_profiles()
        self.ui_settings = self.load_ui_settings()
        self.lighting_state = self.load_lighting_state()
        self.rgb_backend_probe = {
            "typed_backend_available": False,
            "device": None,
        }

        self.build_ui()
        self.apply_style()
        self.setup_tray()
        self.refresh_user_profiles()
        self.apply_ui_settings()
        self.apply_lighting_state_to_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

        QTimer.singleShot(
            700,
            self.reload_history,
        )

        QTimer.singleShot(
            1200,
            self.refresh_rgb_backend,
        )

        self.history_timer = QTimer(self)
        self.history_timer.timeout.connect(
            self.reload_history
        )
        self.history_timer.start(5000)

    def build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QFrame()
        top.setObjectName("topBar")
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(22, 12, 18, 12)

        brand = QLabel("NITRO")
        brand.setObjectName("brand")
        sense = QLabel("CONTROL")
        sense.setObjectName("brandSense")
        model = QLabel("  •  AN515-58")
        model.setObjectName("model")

        top_lay.addWidget(brand)
        top_lay.addWidget(sense)
        top_lay.addWidget(model)
        top_lay.addStretch()

        self.quick_profile = QLabel("BALANCED")
        self.quick_profile.setObjectName("quickProfile")
        self.hotkey_hint = QLabel("NITRO KEY READY")
        self.hotkey_hint.setObjectName("hotkeyHint")
        top_lay.addWidget(self.quick_profile)
        top_lay.addSpacing(12)
        top_lay.addWidget(self.hotkey_hint)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(185)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(12, 18, 12, 18)
        nav_lay.setSpacing(8)

        self.nav_buttons = []
        for text, index in [
            ("MONITORING", 0),
            ("FAN CONTROL", 1),
            ("POWER MODE", 2),
            ("LIGHTING", 3),
            ("SETTINGS", 4),
        ]:
            b = QPushButton(text)
            b.setObjectName("navButton")
            b.setCheckable(True)
            b.clicked.connect(
                lambda _checked=False, i=index: self.select_page(i)
            )
            nav_lay.addWidget(b)
            self.nav_buttons.append(b)

        nav_lay.addStretch()

        version = QLabel(f"KNC {VERSION}")
        version.setObjectName("navVersion")
        nav_lay.addWidget(version)

        body.addWidget(nav)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.build_monitor_page())
        self.stack.addWidget(self.build_fan_page())
        self.stack.addWidget(self.build_power_page())
        self.stack.addWidget(self.build_lighting_page())
        self.stack.addWidget(self.build_settings_page())
        body.addWidget(self.stack, 1)

        root.addLayout(body, 1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.setCentralWidget(central)
        self.select_page(0)

    def panel(self, title):
        frame = QFrame()
        frame.setObjectName("panel")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            14,
            12,
            14,
            14,
        )
        layout.setSpacing(10)

        header = QLabel(title)
        header.setObjectName("panelTitle")
        layout.addWidget(header)

        return frame, layout

    def build_monitor_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel("SYSTEM MONITORING")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        cards = QGridLayout()
        cards.setHorizontalSpacing(10)

        self.cpu_card = SensorCard("CPU")
        self.gpu_card = SensorCard("GPU", "#39a9ff")
        self.cpu_fan_card = SensorCard("CPU FAN")
        self.gpu_fan_card = SensorCard("GPU FAN")
        self.power_card = SensorCard("GPU POWER")
        self.vram_card = SensorCard("VRAM")
        self.cpu_clock_card = SensorCard("CPU CLOCK")
        self.gpu_clock_card = SensorCard("GPU CLOCK", "#39a9ff")
        self.battery_card = SensorCard("BATTERY")

        self.sensor_cards = {
            "cpu": self.cpu_card,
            "gpu": self.gpu_card,
            "cpu_fan": self.cpu_fan_card,
            "gpu_fan": self.gpu_fan_card,
            "gpu_power": self.power_card,
            "vram": self.vram_card,
            "cpu_clock": self.cpu_clock_card,
            "gpu_clock": self.gpu_clock_card,
            "battery": self.battery_card,
        }

        for i, c in enumerate([
            self.cpu_card,
            self.gpu_card,
            self.cpu_fan_card,
            self.gpu_fan_card,
            self.power_card,
            self.vram_card,
            self.cpu_clock_card,
            self.gpu_clock_card,
            self.battery_card,
        ]):
            cards.addWidget(
                c,
                i // 3,
                i % 3,
            )

        layout.addLayout(cards)

        chart_panel, chart_lay = self.panel("TEMPERATURE HISTORY")

        history_bar = QHBoxLayout()
        history_bar.addWidget(QLabel("RANGE"))

        self.history_range = QComboBox()
        self.history_range.addItem("5 MIN", 5)
        self.history_range.addItem("15 MIN", 15)
        self.history_range.addItem("1 HOUR", 60)
        self.history_range.setCurrentIndex(1)
        self.history_range.currentIndexChanged.connect(
            self.reload_history
        )

        history_bar.addWidget(self.history_range)

        refresh_history = QPushButton("LOAD HISTORY")
        refresh_history.clicked.connect(
            self.reload_history
        )
        history_bar.addWidget(refresh_history)
        history_bar.addStretch()

        chart_lay.addLayout(history_bar)

        self.chart = HistoryChart()
        chart_lay.addWidget(self.chart)
        layout.addWidget(chart_panel, 1)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("QUICK MODE"))
        for key, label in [
            ("quiet", "QUIET"),
            ("balanced", "DEFAULT"),
            ("performance", "PERFORMANCE"),
            ("max", "MAX"),
        ]:
            b = QPushButton(label)
            b.setObjectName("presetButton")
            b.clicked.connect(
                lambda _checked=False, k=key: self.apply_preset(k)
            )
            preset_row.addWidget(b)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        return page

    def build_fan_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            20,
            18,
            20,
            20,
        )
        layout.setSpacing(14)

        title = QLabel("FAN CONTROL")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        fan_panel, fan_layout = self.panel(
            "DUAL FAN"
        )

        fan_row = QHBoxLayout()
        fan_row.setSpacing(52)
        fan_row.addStretch()

        self.cpu_dial = FanDial(
            "CPU FAN"
        )
        self.gpu_dial = FanDial(
            "GPU FAN"
        )

        fan_row.addWidget(
            self.cpu_dial,
            0,
            Qt.AlignmentFlag.AlignCenter,
        )
        fan_row.addWidget(
            self.gpu_dial,
            0,
            Qt.AlignmentFlag.AlignCenter,
        )
        fan_row.addStretch()

        fan_layout.addLayout(
            fan_row
        )

        separator = QFrame()
        separator.setObjectName(
            "separator"
        )
        separator.setFrameShape(
            QFrame.Shape.HLine
        )
        fan_layout.addWidget(
            separator
        )

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)

        mode_label = QLabel("MODE")
        mode_label.setObjectName(
            "controlLabel"
        )
        mode_label.setMinimumWidth(
            86
        )
        mode_row.addWidget(
            mode_label
        )

        self.fan_auto = QPushButton(
            "AUTO"
        )
        self.fan_custom = QPushButton(
            "CUSTOM"
        )
        self.fan_max = QPushButton(
            "MAX"
        )

        for button in (
            self.fan_auto,
            self.fan_custom,
            self.fan_max,
        ):
            button.setCheckable(True)
            button.setObjectName(
                "fanModeButton"
            )
            button.setMinimumWidth(
                110
            )
            mode_row.addWidget(
                button
            )

        self.fan_auto.clicked.connect(
            lambda:
                self.set_fan_mode(
                    "auto"
                )
        )
        self.fan_custom.clicked.connect(
            self.apply_manual
        )
        self.fan_max.clicked.connect(
            lambda:
                self.set_fan_mode(
                    "turbo"
                )
        )

        mode_row.addStretch()

        self.smart_curve = QCheckBox(
            "SMART CURVE"
        )
        self.smart_curve.setObjectName(
            "smartCurve"
        )
        self.smart_curve.toggled.connect(
            self.smart_curve_changed
        )
        mode_row.addWidget(
            self.smart_curve
        )

        fan_layout.addLayout(
            mode_row
        )

        boost_row = QHBoxLayout()
        boost_row.setSpacing(8)

        boost_label = QLabel(
            "FAN BOOST"
        )
        boost_label.setObjectName(
            "controlLabel"
        )
        boost_label.setMinimumWidth(
            86
        )
        boost_row.addWidget(
            boost_label
        )

        for seconds, label in (
            (30, "30 SEC"),
            (60, "1 MIN"),
            (180, "3 MIN"),
        ):
            button = QPushButton(
                label
            )
            button.setObjectName(
                "secondaryButton"
            )
            button.setMinimumWidth(
                100
            )
            button.clicked.connect(
                lambda _checked=False, s=seconds:
                    self.start_boost(s)
            )
            boost_row.addWidget(
                button
            )

        self.boost_cancel = QPushButton(
            "CANCEL"
        )
        self.boost_cancel.setObjectName(
            "secondaryButton"
        )
        self.boost_cancel.clicked.connect(
            self.cancel_boost
        )
        boost_row.addWidget(
            self.boost_cancel
        )

        self.boost_status = QLabel(
            "IDLE"
        )
        self.boost_status.setObjectName(
            "statusBadge"
        )
        boost_row.addWidget(
            self.boost_status
        )
        boost_row.addStretch()

        fan_layout.addLayout(
            boost_row
        )

        self.cpu_slider, self.cpu_slider_value = self.slider_row(
            fan_layout,
            "CPU FAN",
            30,
            100,
            50,
        )
        self.gpu_slider, self.gpu_slider_value = self.slider_row(
            fan_layout,
            "GPU FAN",
            30,
            100,
            50,
        )

        self.cpu_slider.sliderReleased.connect(
            self.apply_manual
        )
        self.gpu_slider.sliderReleased.connect(
            self.apply_manual
        )

        note = QLabel(
            "AUTO = firmware Acer   •   "
            "CUSTOM = controle manual   •   "
            "MAX = rotação máxima   •   "
            "SMART CURVE = curva dinâmica Kaevryx"
        )
        note.setWordWrap(True)
        note.setObjectName(
            "hint"
        )
        fan_layout.addWidget(
            note
        )

        layout.addWidget(
            fan_panel
        )

        curve_panel, curve_layout = self.panel(
            "SMART FAN CURVE EDITOR"
        )

        curve_header = QHBoxLayout()
        curve_header.setSpacing(
            8
        )

        edit_label = QLabel(
            "EDIT"
        )
        edit_label.setObjectName(
            "controlLabel"
        )
        edit_label.setMinimumWidth(
            42
        )
        curve_header.addWidget(
            edit_label
        )

        self.curve_series = QComboBox()
        self.curve_series.addItem(
            "CPU FAN",
            "cpu",
        )
        self.curve_series.addItem(
            "GPU FAN",
            "gpu",
        )
        self.curve_series.setMinimumWidth(
            130
        )
        self.curve_series.currentIndexChanged.connect(
            self.curve_series_changed
        )
        curve_header.addWidget(
            self.curve_series
        )

        curve_header.addStretch()

        save_curve = QPushButton(
            "SAVE CURVE"
        )
        save_curve.clicked.connect(
            self.save_fan_curves
        )

        reset_curve = QPushButton(
            "RESET"
        )
        reset_curve.setObjectName(
            "secondaryButton"
        )
        reset_curve.clicked.connect(
            self.reset_fan_curves
        )

        curve_header.addWidget(
            save_curve
        )
        curve_header.addWidget(
            reset_curve
        )

        curve_layout.addLayout(
            curve_header
        )

        self.curve_editor = DualCurveEditor()
        curve_layout.addWidget(
            self.curve_editor
        )

        layout.addWidget(
            curve_panel
        )

        feature_panel, feature_layout = self.panel(
            "COOLBOOST"
        )

        cool_row = QHBoxLayout()

        cool_text = QLabel(
            "O NitroSense original possui CoolBoost, "
            "mas o kernel acer-wmi do AN515-58 não expõe "
            "uma interface CoolBoost separada."
        )
        cool_text.setWordWrap(
            True
        )
        cool_text.setObjectName(
            "hint"
        )

        unavailable = QLabel(
            "NOT EXPOSED"
        )
        unavailable.setObjectName(
            "statusBadgeWarning"
        )

        cool_row.addWidget(
            cool_text,
            1,
        )
        cool_row.addWidget(
            unavailable
        )

        feature_layout.addLayout(
            cool_row
        )
        layout.addWidget(
            feature_panel
        )

        layout.addStretch()

        scroll.setWidget(
            page
        )
        return scroll

    def slider_row(self, parent, label, minimum, maximum, value):
        row = QHBoxLayout()
        name = QLabel(label)
        name.setObjectName("sliderLabel")
        name.setMinimumWidth(95)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)

        value_label = QLabel(f"{value}%")
        value_label.setObjectName("sliderValue")
        value_label.setMinimumWidth(52)

        slider.valueChanged.connect(
            lambda v, target=value_label: target.setText(f"{v}%")
        )

        row.addWidget(name)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        parent.addLayout(row)

        return slider, value_label

    def build_power_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel("OPERATING MODE")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        mode_panel, mode_layout = self.panel("ACER PERFORMANCE PROFILE")

        description = QLabel(
            "Perfis reais fornecidos pelo firmware Acer via platform_profile."
        )
        description.setObjectName("hint")
        mode_layout.addWidget(description)

        self.owner = QComboBox()
        self.owner.addItem("KDE COMPATIBILITY / 3 PROFILES", "kde")
        self.owner.addItem("ACER FULL CONTROL / 5 PROFILES", "acer")
        self.owner.currentIndexChanged.connect(self.owner_changed)
        mode_layout.addWidget(self.owner)

        self.profile_buttons = {}
        grid = QGridLayout()

        profiles = [
            ("low-power", "LOW POWER", "Menor consumo"),
            ("quiet", "QUIET", "Menos ruído"),
            ("balanced", "DEFAULT", "Uso geral"),
            ("balanced-performance", "PERFORMANCE", "Maior resposta"),
            ("performance", "PERFORMANCE+", "Máximo firmware"),
        ]

        for i, (key, label, desc) in enumerate(profiles):
            b = QPushButton(f"{label}\n{desc}")
            b.setObjectName("profileButton")
            b.setCheckable(True)
            b.clicked.connect(
                lambda _checked=False, p=key: self.select_profile(p)
            )
            grid.addWidget(b, i // 2, i % 2)
            self.profile_buttons[key] = b

        mode_layout.addLayout(grid)
        layout.addWidget(mode_panel)

        info_panel, info_layout = self.panel("ORIGINAL NITROSENSE PROFILE DATA")
        info = QLabel(
            "O pacote Acer do AN515-58 declara suporte a CPU Fan, GPU Fan, "
            "teclado 4 zonas, Temperature, LCD/Animation e presets de GPU. "
            "Neste Linux, somente funções com backend seguro detectado são habilitadas."
        )
        info.setWordWrap(True)
        info.setObjectName("hint")
        info_layout.addWidget(info)
        layout.addWidget(info_panel)

        profiles_panel, profiles_layout = self.panel("MY PROFILES")
        profile_bar = QHBoxLayout()
        self.user_profile_list = QListWidget()
        self.user_profile_list.setMaximumHeight(130)
        profile_bar.addWidget(self.user_profile_list, 1)

        profile_actions = QVBoxLayout()
        save_profile = QPushButton("SAVE CURRENT")
        apply_profile = QPushButton("APPLY")
        delete_profile = QPushButton("DELETE")
        save_profile.clicked.connect(self.save_current_user_profile)
        apply_profile.clicked.connect(self.apply_selected_user_profile)
        delete_profile.clicked.connect(self.delete_selected_user_profile)
        profile_actions.addWidget(save_profile)
        profile_actions.addWidget(apply_profile)
        profile_actions.addWidget(delete_profile)
        profile_actions.addStretch()
        profile_bar.addLayout(profile_actions)

        profiles_layout.addLayout(profile_bar)
        layout.addWidget(profiles_panel)

        layout.addStretch()

        return page

    def build_lighting_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            20,
            18,
            20,
            20,
        )
        layout.setSpacing(14)

        title = QLabel(
            "LIGHTING STUDIO"
        )
        title.setObjectName(
            "pageTitle"
        )
        layout.addWidget(
            title
        )

        preview_panel, preview_layout = self.panel(
            "4-ZONE KEYBOARD"
        )

        self.zone_preview = ZonePreview()
        preview_layout.addWidget(
            self.zone_preview
        )

        zone_row = QHBoxLayout()
        zone_row.setSpacing(
            8
        )
        self.zone_buttons = []

        for index in range(4):
            button = QPushButton(
                f"ZONE {index + 1}"
            )
            button.setObjectName(
                "zoneColorButton"
            )
            button.setMinimumHeight(
                38
            )
            button.clicked.connect(
                lambda _checked=False, i=index:
                    self.choose_zone_color(i)
            )

            self.zone_buttons.append(
                button
            )
            zone_row.addWidget(
                button,
                1,
            )

        preview_layout.addLayout(
            zone_row
        )

        controls = QGridLayout()
        controls.setHorizontalSpacing(
            10
        )
        controls.setVerticalSpacing(
            10
        )

        effect_label = QLabel(
            "EFFECT"
        )
        effect_label.setObjectName(
            "controlLabel"
        )

        self.lighting_effect = QComboBox()
        self.lighting_effect.addItem(
            "STATIC",
            "static",
        )
        self.lighting_effect.addItem(
            "BREATHING",
            "breathing",
        )
        self.lighting_effect.addItem(
            "WAVE",
            "wave",
        )
        self.lighting_effect.addItem(
            "TEMPERATURE",
            "temperature",
        )
        self.lighting_effect.setMinimumWidth(
            145
        )
        self.lighting_effect.currentIndexChanged.connect(
            self.lighting_effect_changed
        )

        speed_label = QLabel(
            "SPEED"
        )
        speed_label.setObjectName(
            "controlLabel"
        )

        self.lighting_speed = QSlider(
            Qt.Orientation.Horizontal
        )
        self.lighting_speed.setRange(
            0,
            100,
        )
        self.lighting_speed.setValue(
            50
        )
        self.lighting_speed.valueChanged.connect(
            self.lighting_speed_changed
        )

        self.lighting_speed_value = QLabel(
            "50%"
        )
        self.lighting_speed_value.setObjectName(
            "sliderValue"
        )
        self.lighting_speed_value.setMinimumWidth(
            48
        )

        controls.addWidget(
            effect_label,
            0,
            0,
        )
        controls.addWidget(
            self.lighting_effect,
            0,
            1,
        )
        controls.addWidget(
            speed_label,
            0,
            2,
        )
        controls.addWidget(
            self.lighting_speed,
            0,
            3,
        )
        controls.addWidget(
            self.lighting_speed_value,
            0,
            4,
        )

        backlight_label = QLabel(
            "BACKLIGHT"
        )
        backlight_label.setObjectName(
            "controlLabel"
        )

        self.keyboard_slider = QSlider(
            Qt.Orientation.Horizontal
        )
        self.keyboard_slider.setRange(
            0,
            100,
        )
        self.keyboard_slider.setValue(
            100
        )
        self.keyboard_slider.sliderReleased.connect(
            self.apply_keyboard_brightness
        )

        self.keyboard_value = QLabel(
            "100%"
        )
        self.keyboard_value.setObjectName(
            "sliderValue"
        )
        self.keyboard_value.setMinimumWidth(
            48
        )

        self.keyboard_slider.valueChanged.connect(
            lambda value:
                self.keyboard_value.setText(
                    f"{value}%"
                )
        )

        controls.addWidget(
            backlight_label,
            1,
            0,
        )
        controls.addWidget(
            self.keyboard_slider,
            1,
            1,
            1,
            3,
        )
        controls.addWidget(
            self.keyboard_value,
            1,
            4,
        )

        controls.setColumnStretch(
            3,
            1,
        )

        preview_layout.addLayout(
            controls
        )

        self.keyboard_status = QLabel(
            "Detecting keyboard backlight backend..."
        )
        self.keyboard_status.setObjectName(
            "backendStatus"
        )
        self.keyboard_status.setWordWrap(
            True
        )
        preview_layout.addWidget(
            self.keyboard_status
        )

        layout.addWidget(
            preview_panel
        )

        preset_panel, preset_layout = self.panel(
            "LIGHTING PRESETS"
        )

        builtin_row = QHBoxLayout()
        builtin_row.setSpacing(
            8
        )

        for name in (
            "Nitro Red",
            "Ice Blue",
            "Cyber",
            "Purple",
        ):
            button = QPushButton(
                name.upper()
            )
            button.setObjectName(
                "secondaryButton"
            )
            button.clicked.connect(
                lambda _checked=False, n=name:
                    self.apply_builtin_lighting_preset(n)
            )
            builtin_row.addWidget(
                button
            )

        builtin_row.addStretch()
        preset_layout.addLayout(
            builtin_row
        )

        custom_create = QHBoxLayout()
        custom_create.setSpacing(
            8
        )

        self.lighting_preset_name = QLineEdit()
        self.lighting_preset_name.setPlaceholderText(
            "Custom preset name"
        )

        save_preset = QPushButton(
            "SAVE CUSTOM"
        )
        save_preset.clicked.connect(
            self.save_lighting_preset
        )

        custom_create.addWidget(
            self.lighting_preset_name,
            1,
        )
        custom_create.addWidget(
            save_preset
        )

        preset_layout.addLayout(
            custom_create
        )

        custom_apply = QHBoxLayout()
        custom_apply.setSpacing(
            8
        )

        self.lighting_preset_combo = QComboBox()

        apply_custom = QPushButton(
            "APPLY SAVED"
        )
        apply_custom.clicked.connect(
            self.apply_saved_lighting_preset
        )

        delete_custom = QPushButton(
            "DELETE"
        )
        delete_custom.setObjectName(
            "secondaryButton"
        )
        delete_custom.clicked.connect(
            self.delete_saved_lighting_preset
        )

        custom_apply.addWidget(
            self.lighting_preset_combo,
            1,
        )
        custom_apply.addWidget(
            apply_custom
        )
        custom_apply.addWidget(
            delete_custom
        )

        preset_layout.addLayout(
            custom_apply
        )

        layout.addWidget(
            preset_panel
        )

        feature_panel, feature_layout = self.panel(
            "LINUX RGB BACKEND"
        )

        reference = QLabel(
            "O KNC aplica brilho quando o kernel expõe "
            "kbd_backlight. Cores RGB físicas só são habilitadas "
            "quando um backend Linux tipado e compatível é detectado."
        )
        reference.setWordWrap(
            True
        )
        reference.setObjectName(
            "hint"
        )
        feature_layout.addWidget(
            reference
        )

        self.rgb_backend_status = QLabel(
            "RGB BACKEND: DISCOVERY PENDING"
        )
        self.rgb_backend_status.setObjectName(
            "statusBadgeWarning"
        )
        self.rgb_backend_status.setWordWrap(
            True
        )
        feature_layout.addWidget(
            self.rgb_backend_status
        )

        backend_buttons = QHBoxLayout()
        backend_buttons.setSpacing(
            8
        )

        discover = QPushButton(
            "DISCOVER"
        )
        discover.clicked.connect(
            self.refresh_rgb_backend
        )

        self.apply_rgb_hardware_button = QPushButton(
            "APPLY TO HARDWARE"
        )
        self.apply_rgb_hardware_button.setEnabled(
            False
        )
        self.apply_rgb_hardware_button.clicked.connect(
            self.apply_rgb_to_hardware
        )

        self.rgb_power_on_button = QPushButton(
            "LIGHTS ON"
        )
        self.rgb_power_on_button.setEnabled(
            False
        )
        self.rgb_power_on_button.clicked.connect(
            lambda:
                self.set_rgb_hardware_power(
                    True
                )
        )

        self.rgb_power_off_button = QPushButton(
            "LIGHTS OFF"
        )
        self.rgb_power_off_button.setEnabled(
            False
        )
        self.rgb_power_off_button.clicked.connect(
            lambda:
                self.set_rgb_hardware_power(
                    False
                )
        )

        backend_buttons.addWidget(
            discover
        )
        backend_buttons.addWidget(
            self.apply_rgb_hardware_button
        )
        backend_buttons.addWidget(
            self.rgb_power_on_button
        )
        backend_buttons.addWidget(
            self.rgb_power_off_button
        )
        backend_buttons.addStretch()

        feature_layout.addLayout(
            backend_buttons
        )

        layout.addWidget(
            feature_panel
        )
        layout.addStretch()

        scroll.setWidget(
            page
        )
        return scroll

    def build_settings_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            20,
            18,
            20,
            18,
        )
        layout.setSpacing(14)

        title = QLabel("CONTROL CENTER")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        system_panel, system_layout = self.panel(
            "KAEVRYX BACKEND"
        )
        self.backend_info = QLabel(
            "Carregando..."
        )
        self.backend_info.setObjectName("mono")
        self.backend_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        system_layout.addWidget(
            self.backend_info
        )
        layout.addWidget(system_panel)

        ui_panel, ui_layout = self.panel(
            "UI / NITRO KEY"
        )

        ui_row = QHBoxLayout()

        self.osd_checkbox = QCheckBox(
            "OSD ENABLED"
        )
        self.osd_checkbox.setObjectName(
            "smartCurve"
        )

        self.close_tray_checkbox = QCheckBox(
            "CLOSE TO TRAY"
        )
        self.close_tray_checkbox.setObjectName(
            "smartCurve"
        )

        self.hotkey_action_combo = QComboBox()
        self.hotkey_action_combo.addItem(
            "BALANCED ↔ GAMING",
            "toggle_gaming",
        )
        self.hotkey_action_combo.addItem(
            "FAN BOOST 60 SEC",
            "boost60",
        )
        self.hotkey_action_combo.addItem(
            "MAX MODE",
            "max",
        )
        self.hotkey_action_combo.addItem(
            "BALANCED MODE",
            "balanced",
        )
        self.hotkey_action_combo.addItem(
            "NO DOUBLE-TAP ACTION",
            "none",
        )

        save_ui = QPushButton(
            "APPLY UI SETTINGS"
        )
        save_ui.clicked.connect(
            self.save_ui_settings_from_widgets
        )

        ui_row.addWidget(
            self.osd_checkbox
        )
        ui_row.addWidget(
            self.close_tray_checkbox
        )
        ui_row.addWidget(
            QLabel("DOUBLE NITRO")
        )
        ui_row.addWidget(
            self.hotkey_action_combo,
            1,
        )
        ui_row.addWidget(save_ui)

        ui_layout.addLayout(ui_row)

        hotkey_note = QLabel(
            "Um toque continua abrindo o KNC. "
            "A ação acima controla apenas o duplo toque."
        )
        hotkey_note.setObjectName("hint")
        ui_layout.addWidget(hotkey_note)

        layout.addWidget(ui_panel)

        monitor_panel, monitor_layout = self.panel(
            "MONITORING DISPLAY"
        )

        monitor_top = QHBoxLayout()
        monitor_top.addWidget(
            QLabel("TEMPERATURE WARNING")
        )

        self.cpu_warning_spin = QSpinBox()
        self.cpu_warning_spin.setRange(
            60,
            100,
        )
        self.cpu_warning_spin.setSuffix(
            " °C CPU"
        )

        self.gpu_warning_spin = QSpinBox()
        self.gpu_warning_spin.setRange(
            60,
            100,
        )
        self.gpu_warning_spin.setSuffix(
            " °C GPU"
        )

        monitor_top.addWidget(
            self.cpu_warning_spin
        )
        monitor_top.addWidget(
            self.gpu_warning_spin
        )
        monitor_top.addStretch()

        monitor_layout.addLayout(
            monitor_top
        )

        card_row = QHBoxLayout()
        self.sensor_visibility_checks = {}

        for key, label in [
            ("cpu", "CPU"),
            ("gpu", "GPU"),
            ("cpu_fan", "CPU FAN"),
            ("gpu_fan", "GPU FAN"),
            ("gpu_power", "GPU POWER"),
            ("vram", "VRAM"),
            ("cpu_clock", "CPU CLOCK"),
            ("gpu_clock", "GPU CLOCK"),
            ("battery", "BATTERY"),
        ]:
            checkbox = QCheckBox(label)
            checkbox.setObjectName(
                "smartCurve"
            )
            self.sensor_visibility_checks[
                key
            ] = checkbox
            card_row.addWidget(
                checkbox
            )

        card_row.addStretch()
        monitor_layout.addLayout(card_row)

        save_monitor = QPushButton(
            "APPLY MONITOR SETTINGS"
        )
        save_monitor.clicked.connect(
            self.save_ui_settings_from_widgets
        )
        monitor_layout.addWidget(
            save_monitor
        )

        layout.addWidget(monitor_panel)

        power_auto_panel, power_auto_layout = self.panel(
            "AUTOMATIC AC / BATTERY PROFILE"
        )
        power_auto_row = QHBoxLayout()

        self.auto_power_checkbox = QCheckBox(
            "ENABLE"
        )
        self.auto_power_checkbox.setObjectName(
            "smartCurve"
        )

        self.ac_preset_combo = QComboBox()
        self.battery_preset_combo = QComboBox()

        for key, label in [
            ("quiet", "QUIET"),
            ("balanced", "BALANCED"),
            ("performance", "PERFORMANCE"),
            ("gaming", "GAMING"),
            ("max", "MAX"),
        ]:
            self.ac_preset_combo.addItem(
                label,
                key,
            )
            self.battery_preset_combo.addItem(
                label,
                key,
            )

        power_auto_row.addWidget(
            self.auto_power_checkbox
        )
        power_auto_row.addWidget(
            QLabel("ON AC")
        )
        power_auto_row.addWidget(
            self.ac_preset_combo
        )
        power_auto_row.addWidget(
            QLabel("ON BATTERY")
        )
        power_auto_row.addWidget(
            self.battery_preset_combo
        )

        save_power_auto = QPushButton(
            "APPLY"
        )
        save_power_auto.clicked.connect(
            self.save_power_automation
        )
        power_auto_row.addWidget(
            save_power_auto
        )

        power_auto_layout.addLayout(
            power_auto_row
        )
        layout.addWidget(
            power_auto_panel
        )

        app_panel, app_layout = self.panel(
            "APPLICATION PROFILE SWITCHING"
        )

        app_top = QHBoxLayout()

        self.app_auto_checkbox = QCheckBox(
            "ENABLE"
        )
        self.app_auto_checkbox.setObjectName(
            "smartCurve"
        )

        self.app_process = QLineEdit()
        self.app_process.setPlaceholderText(
            "process name, e.g. steam, lutris, blender"
        )

        self.app_preset_combo = QComboBox()

        for key, label in [
            ("quiet", "QUIET"),
            ("balanced", "BALANCED"),
            ("performance", "PERFORMANCE"),
            ("gaming", "GAMING"),
            ("max", "MAX"),
        ]:
            self.app_preset_combo.addItem(
                label,
                key,
            )

        add_rule = QPushButton(
            "ADD RULE"
        )
        add_rule.clicked.connect(
            self.add_app_rule
        )

        app_top.addWidget(
            self.app_auto_checkbox
        )
        app_top.addWidget(
            self.app_process,
            1,
        )
        app_top.addWidget(
            self.app_preset_combo
        )
        app_top.addWidget(
            add_rule
        )

        app_layout.addLayout(
            app_top
        )

        self.app_rules_list = QListWidget()
        self.app_rules_list.setMaximumHeight(
            120
        )
        app_layout.addWidget(
            self.app_rules_list
        )

        app_bottom = QHBoxLayout()

        remove_rule = QPushButton(
            "REMOVE SELECTED"
        )
        remove_rule.clicked.connect(
            self.remove_app_rule
        )

        apply_rules = QPushButton(
            "APPLY RULES"
        )
        apply_rules.clicked.connect(
            self.save_app_automation
        )

        self.app_fallback_combo = QComboBox()

        for key, label in [
            ("quiet", "QUIET"),
            ("balanced", "BALANCED"),
            ("performance", "PERFORMANCE"),
            ("gaming", "GAMING"),
            ("max", "MAX"),
        ]:
            self.app_fallback_combo.addItem(
                label,
                key,
            )

        app_bottom.addWidget(
            remove_rule
        )
        app_bottom.addStretch()
        app_bottom.addWidget(
            QLabel("FALLBACK")
        )
        app_bottom.addWidget(
            self.app_fallback_combo
        )
        app_bottom.addWidget(
            apply_rules
        )

        app_layout.addLayout(
            app_bottom
        )
        layout.addWidget(
            app_panel
        )

        export_panel, export_layout = self.panel(
            "DATA / SUPPORT"
        )

        export_row = QHBoxLayout()

        support = QPushButton(
            "EXPORT SUPPORT PACKAGE"
        )
        support.clicked.connect(
            self.export_support_package
        )

        telemetry = QPushButton(
            "EXPORT TELEMETRY CSV"
        )
        telemetry.clicked.connect(
            self.export_telemetry_csv
        )

        reports = QPushButton(
            "OPEN REPORTS FOLDER"
        )
        reports.clicked.connect(
            self.open_reports_folder
        )

        doctor = QPushButton(
            "RUN DIAGNOSTIC"
        )
        doctor.clicked.connect(
            self.run_doctor
        )

        hotkey = QPushButton(
            "TEST NITRO KEY BRIDGE"
        )
        hotkey.clicked.connect(
            self.test_hotkey
        )

        export_row.addWidget(
            support
        )
        export_row.addWidget(
            telemetry
        )
        export_row.addWidget(
            reports
        )
        export_row.addWidget(
            doctor
        )
        export_row.addWidget(
            hotkey
        )
        export_row.addStretch()

        export_layout.addLayout(
            export_row
        )

        export_note = QLabel(
            "Support ZIP inclui estado do KNC, diagnóstico, "
            "telemetria CSV, logs e resultado do RGB backend probe."
        )
        export_note.setWordWrap(True)
        export_note.setObjectName(
            "hint"
        )
        export_layout.addWidget(
            export_note
        )

        layout.addWidget(
            export_panel
        )

        source_panel, source_layout = self.panel(
            "NITROSENSE 3.01.3056 REFERENCE"
        )

        source = QLabel(
            "A interface preserva a estrutura de uso do NitroSense, "
            "mas os elementos visuais e o backend Linux são próprios "
            "do Kaevryx Nitro Control."
        )
        source.setWordWrap(True)
        source.setObjectName("hint")
        source_layout.addWidget(source)

        layout.addWidget(
            source_panel
        )
        layout.addStretch()

        scroll.setWidget(page)
        return scroll

    def select_page(self, index):
        self.stack.setCurrentIndex(index)
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #0d0f11;
                color: #eceff1;
                font-family: "Noto Sans", "DejaVu Sans";
                font-size: 10pt;
            }

            QLabel {
                background: transparent;
                border: none;
                outline: none;
            }

            QFrame#topBar {
                background: #08090a;
                border-bottom: 1px solid #2b2e32;
            }

            QLabel#brand {
                color: #e21d2f;
                font-size: 23pt;
                font-weight: 900;
                letter-spacing: 3px;
            }

            QLabel#brandSense {
                color: #f4f5f6;
                font-size: 20pt;
                font-weight: 300;
                letter-spacing: 1px;
            }

            QLabel#model {
                color: #737a81;
                font-weight: 700;
            }

            QLabel#quickProfile {
                color: #ffffff;
                background: #1b1e22;
                border: 1px solid #3c4147;
                border-left: 3px solid #e21d2f;
                padding: 7px 12px;
                font-weight: 800;
            }

            QLabel#hotkeyHint {
                color: #8e969e;
                font-size: 9pt;
            }

            QFrame#nav {
                background: #111316;
                border-right: 1px solid #26292d;
            }

            QPushButton#navButton {
                text-align: left;
                background: transparent;
                color: #8f969d;
                border: none;
                border-left: 3px solid transparent;
                border-radius: 0px;
                padding: 12px 12px;
                font-weight: 700;
            }

            QPushButton#navButton:hover {
                color: #ffffff;
                background: #171a1d;
            }

            QPushButton#navButton:checked {
                color: #ffffff;
                background: #1a1d21;
                border-left: 3px solid #e21d2f;
            }

            QLabel#navVersion {
                color: #52575d;
                font-size: 8.5pt;
                padding: 8px;
            }

            QLabel#pageTitle {
                color: #ffffff;
                font-size: 18pt;
                font-weight: 800;
                letter-spacing: 1px;
            }

            QFrame#panel, QFrame#sensorCard {
                background: #15181b;
                border: 1px solid #2b2f34;
                border-radius: 6px;
            }

            QFrame#separator {
                background: #2a2e33;
                border: none;
                max-height: 1px;
            }

            QFrame#sensorCard {
                border-top: 2px solid #e21d2f;
            }

            QFrame#sensorCard[alert="true"] {
                border: 1px solid #d99022;
                border-top: 3px solid #ffad2f;
                background: #201810;
            }

            QLabel#panelTitle {
                color: #e21d2f;
                font-weight: 800;
                font-size: 10.5pt;
                letter-spacing: 1px;
            }

            QLabel#sensorTitle {
                color: #969da4;
                font-size: 8.5pt;
                font-weight: 700;
            }

            QLabel#sensorValue {
                color: #ffffff;
                font-size: 18pt;
                font-weight: 800;
            }

            QLabel#sensorDetail, QLabel#hint {
                color: #7f878f;
                font-size: 9pt;
            }

            QLabel#controlLabel {
                color: #aeb4ba;
                font-size: 9pt;
                font-weight: 800;
            }

            QLabel#backendStatus {
                color: #7f878f;
                background: #111417;
                border: 1px solid #2c3136;
                border-radius: 3px;
                padding: 7px 9px;
                font-size: 9pt;
            }

            QLabel#statusBadge {
                color: #d8dde2;
                background: #121518;
                border: 1px solid #3a4046;
                border-radius: 3px;
                padding: 7px 10px;
                font-weight: 800;
            }

            QLabel#statusBadgeWarning {
                color: #f1b24d;
                background: #19150d;
                border: 1px solid #70501a;
                border-radius: 3px;
                padding: 7px 10px;
                font-weight: 800;
            }

            QPushButton {
                color: #e7e9eb;
                background: #1d2024;
                border: 1px solid #383d43;
                border-radius: 3px;
                padding: 9px 13px;
                font-weight: 700;
            }

            QPushButton:hover {
                border-color: #e21d2f;
                color: #ffffff;
            }

            QPushButton:checked {
                color: #ffffff;
                background: #b81425;
                border-color: #e21d2f;
            }

            QPushButton#presetButton {
                min-width: 112px;
            }

            QPushButton#fanModeButton {
                min-width: 105px;
                min-height: 34px;
            }

            QPushButton#secondaryButton {
                background: #181b1f;
                color: #cfd4d8;
                border: 1px solid #343a40;
                min-height: 34px;
            }

            QPushButton#secondaryButton:hover {
                background: #22262b;
                border-color: #59616a;
            }

            QPushButton#profileButton {
                min-height: 72px;
                text-align: left;
                padding-left: 16px;
            }

            QPushButton#zoneColorButton {
                min-height: 42px;
                border-radius: 4px;
            }

            QLineEdit, QListWidget {
                background: #111417;
                border: 1px solid #34393f;
                border-radius: 3px;
                color: #e9ecef;
                padding: 6px;
                selection-background-color: #b81425;
            }

            QListWidget::item {
                padding: 6px;
                border-bottom: 1px solid #24282c;
            }

            QListWidget::item:selected {
                background: #7f1420;
                color: #ffffff;
            }

            QSpinBox {
                background: #171a1d;
                border: 1px solid #3a3e43;
                border-radius: 3px;
                padding: 6px;
                color: #ffffff;
                min-width: 90px;
            }

            QScrollArea {
                border: none;
                background: #0d0f11;
            }

            QComboBox {
                background: #171a1d;
                border: 1px solid #3a3e43;
                border-left: 3px solid #e21d2f;
                border-radius: 3px;
                padding: 8px;
                color: #ffffff;
            }

            QSlider::groove:horizontal {
                height: 6px;
                background: #292e33;
                border-radius: 3px;
            }

            QSlider::sub-page:horizontal {
                background: #e21d2f;
                border-radius: 3px;
            }

            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: #f5f6f7;
                border: 1px solid #e21d2f;
            }

            QLabel#sliderLabel {
                color: #afb5bb;
                font-weight: 700;
            }

            QLabel#sliderValue {
                color: #ffffff;
                font-weight: 800;
            }

            QCheckBox#smartCurve {
                color: #ffffff;
                font-weight: 800;
                spacing: 8px;
            }

            QCheckBox#smartCurve::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #4a5158;
                border-radius: 3px;
                background: #101214;
            }

            QCheckBox#smartCurve::indicator:checked {
                background: #c91427;
                border-color: #e21d2f;
            }

            QScrollBar:vertical {
                background: #0f1113;
                width: 10px;
                margin: 0px;
            }

            QScrollBar::handle:vertical {
                background: #353b42;
                border-radius: 5px;
                min-height: 36px;
            }

            QScrollBar::handle:vertical:hover {
                background: #4a525b;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }

            QLabel#unavailable {
                color: #f1b24d;
                background: #19150d;
                border: 1px solid #70501a;
                border-radius: 3px;
                padding: 7px 10px;
                font-weight: 800;
            }

            QLabel#mono {
                color: #b8c0c8;
                font-family: "Fira Code", "DejaVu Sans Mono";
            }

            QStatusBar {
                background: #090a0b;
                color: #7f878f;
                border-top: 1px solid #282b2f;
            }
        """)

    def show_error(self, message):
        self.status_bar.showMessage(message, 7000)
        QMessageBox.warning(self, "Kaevryx Nitro Control", message)

    def show_osd(self, title, detail=""):
        if not self.osd_enabled():
            return

        try:
            subprocess.Popen(
                [
                    "/usr/bin/python3",
                    "/opt/kaevryx-nitro-control/knc_osd.py",
                    str(title),
                    str(detail),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

    def start_boost(self, seconds):
        try:
            request({
                "cmd": "fan_boost",
                "seconds": int(seconds),
            })
            self.show_osd(
                "FAN BOOST",
                f"Maximum fan for {seconds} seconds",
            )
            self.refresh()

        except KNCError as exc:
            self.show_error(str(exc))

    def cancel_boost(self):
        try:
            request({
                "cmd": "cancel_fan_boost",
            })
            self.show_osd(
                "FAN BOOST",
                "Cancelled",
            )
            self.refresh()

        except KNCError as exc:
            self.show_error(str(exc))

    def reload_history(self, *_args):
        if not hasattr(self, "history_range"):
            return

        try:
            minutes = int(
                self.history_range.currentData()
            )

            data = request({
                "cmd": "history",
                "minutes": minutes,
            })

            samples = data.get(
                "samples",
                [],
            )

            self.chart.set_range_minutes(minutes)
            self.chart.set_samples(samples)

            self.status_bar.showMessage(
                f"History: {len(samples)} samples / "
                f"{minutes} min.",
                2500,
            )

        except KNCError as exc:
            # Do not erase the last valid graph when one history
            # refresh fails temporarily.
            self.status_bar.showMessage(
                f"History unavailable: {exc}",
                5000,
            )


    def refresh_rgb_backend(self, *_args):
        try:
            probe = probe_rgb_backend()
        except Exception as exc:
            probe = {
                "typed_backend_available": False,
                "device": None,
                "wmi_guid_present": False,
                "asense_socket_present": False,
                "error": str(exc),
            }

        self.rgb_backend_probe = probe

        available = bool(
            probe.get(
                "typed_backend_available"
            )
        )

        device = probe.get(
            "device"
        )

        self.apply_rgb_hardware_button.setEnabled(
            available
        )
        self.rgb_power_on_button.setEnabled(
            available
        )
        self.rgb_power_off_button.setEnabled(
            available
        )

        if available and isinstance(
            device,
            dict,
        ):
            backend = device.get(
                "backend",
                "typed",
            )
            zones = device.get(
                "zones",
                "?",
            )
            device_id = device.get(
                "id",
                "?",
            )

            self.rgb_backend_status.setText(
                "RGB BACKEND: ACTIVE  •  "
                f"{backend}  •  "
                f"{zones} zone(s)  •  "
                f"id={device_id}"
            )
            return

        if probe.get(
            "asense_socket_present"
        ) and probe.get(
            "error"
        ):
            self.rgb_backend_status.setText(
                "RGB BACKEND: SERVICE FOUND, "
                "BUT DISCOVERY FAILED  •  "
                + str(
                    probe.get("error")
                )
            )
            return

        if probe.get(
            "wmi_guid_present"
        ):
            self.rgb_backend_status.setText(
                "RGB BACKEND: ACER ZONED-WMI CANDIDATE DETECTED, "
                "BUT NO TYPED CONTROL BACKEND IS AVAILABLE. "
                "PREVIEW ONLY."
            )
            return

        self.rgb_backend_status.setText(
            "RGB BACKEND: NOT DETECTED  •  PREVIEW ONLY"
        )

    def apply_rgb_to_hardware(self):
        effect = self.lighting_state.get(
            "effect",
            "static",
        )

        if effect == "temperature":
            self.show_error(
                "Temperature Lighting permanece preview-only. "
                "Use Static, Breathing ou Wave para aplicar ao hardware."
            )
            return

        try:
            result = apply_lighting(
                colors=list(
                    self.lighting_state.get(
                        "colors",
                        [],
                    )
                ),
                effect=effect,
                brightness=int(
                    self.keyboard_slider.value()
                ),
                speed_percent=int(
                    self.lighting_state.get(
                        "speed",
                        50,
                    )
                ),
            )

            device = result.get(
                "device",
                {},
            )

            self.status_bar.showMessage(
                "RGB aplicado via backend tipado "
                f"({device.get('backend', 'backend')}).",
                5000,
            )

            self.show_osd(
                "RGB LIGHTING",
                f"{effect.upper()} applied",
            )

            self.refresh_rgb_backend()

        except RGBBackendError as exc:
            self.show_error(
                str(exc)
            )

        except Exception as exc:
            self.show_error(
                f"RGB backend: {exc}"
            )

    def set_rgb_hardware_power(self, enabled):
        try:
            power_lighting(
                bool(enabled)
            )

            self.status_bar.showMessage(
                "Keyboard lighting "
                + (
                    "enabled."
                    if enabled
                    else "disabled."
                ),
                4000,
            )

        except RGBBackendError as exc:
            self.show_error(
                str(exc)
            )

        except Exception as exc:
            self.show_error(
                f"RGB power backend: {exc}"
            )

    def default_lighting_state(self):
        return {
            "colors": [
                "#ff0000",
                "#ff0000",
                "#ff0000",
                "#ff0000",
            ],
            "effect": "static",
            "speed": 50,
            "brightness": 100,
            "presets": {},
        }

    def load_lighting_state(self):
        defaults = self.default_lighting_state()

        try:
            value = json.loads(
                self.lighting_file.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(
                value,
                dict,
            ):
                return defaults

            result = {
                **defaults,
                **value,
            }

            colors = result.get(
                "colors",
                defaults["colors"],
            )

            if (
                not isinstance(colors, list)
                or len(colors) != 4
            ):
                result["colors"] = list(
                    defaults["colors"]
                )

            if not isinstance(
                result.get("presets"),
                dict,
            ):
                result["presets"] = {}

            return result

        except Exception:
            return defaults

    def save_lighting_state(self):
        tmp = self.lighting_file.with_suffix(
            ".tmp"
        )

        tmp.write_text(
            json.dumps(
                self.lighting_state,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        tmp.replace(
            self.lighting_file
        )

    def refresh_lighting_preset_combo(self):
        if not hasattr(
            self,
            "lighting_preset_combo",
        ):
            return

        self.lighting_preset_combo.clear()

        presets = self.lighting_state.get(
            "presets",
            {},
        )

        for name in sorted(presets):
            self.lighting_preset_combo.addItem(
                name,
                name,
            )

    def apply_lighting_state_to_ui(self):
        if not hasattr(
            self,
            "zone_preview",
        ):
            return

        colors = self.lighting_state.get(
            "colors",
            [
                "#ff0000",
                "#ff0000",
                "#ff0000",
                "#ff0000",
            ],
        )

        self.zone_preview.set_colors(
            colors
        )

        for index, button in enumerate(
            self.zone_buttons
        ):
            color = QColor(
                colors[index]
            )

            button.setStyleSheet(
                "QPushButton {"
                "background:#1d2024;"
                "color:#f0f2f4;"
                "border:1px solid #383d43;"
                f"border-left:6px solid {color.name()};"
                "border-radius:4px;"
                "font-weight:800;"
                "padding:8px 12px;"
                "}"
                "QPushButton:hover {"
                "background:#24282d;"
                f"border-color:{color.name()};"
                "}"
            )

        effect = self.lighting_state.get(
            "effect",
            "static",
        )

        effect_index = (
            self.lighting_effect.findData(
                effect
            )
        )

        if effect_index >= 0:
            self.lighting_effect.setCurrentIndex(
                effect_index
            )

        speed = int(
            self.lighting_state.get(
                "speed",
                50,
            )
        )
        self.lighting_speed.setValue(
            speed
        )
        self.zone_preview.set_speed(
            speed
        )
        self.zone_preview.set_effect(
            effect
        )

        brightness = int(
            self.lighting_state.get(
                "brightness",
                100,
            )
        )

        self.keyboard_slider.setValue(
            brightness
        )

        self.refresh_lighting_preset_combo()

    def choose_zone_color(self, index):
        colors = list(
            self.lighting_state.get(
                "colors",
                [
                    "#ff0000",
                    "#ff0000",
                    "#ff0000",
                    "#ff0000",
                ],
            )
        )

        current = QColor(
            colors[index]
        )

        color = QColorDialog.getColor(
            current,
            self,
            f"Zone {index + 1} color",
        )

        if not color.isValid():
            return

        colors[index] = color.name(
            QColor.NameFormat.HexRgb
        )

        self.lighting_state[
            "colors"
        ] = colors

        self.save_lighting_state()
        self.apply_lighting_state_to_ui()

    def lighting_effect_changed(self, *_args):
        if not hasattr(
            self,
            "lighting_effect",
        ):
            return

        effect = self.lighting_effect.currentData()

        self.lighting_state[
            "effect"
        ] = effect

        self.zone_preview.set_effect(
            effect
        )

        self.save_lighting_state()

    def lighting_speed_changed(self, value):
        if not hasattr(
            self,
            "lighting_speed_value",
        ):
            return

        self.lighting_speed_value.setText(
            f"{value}%"
        )

        self.lighting_state[
            "speed"
        ] = int(value)

        self.zone_preview.set_speed(
            value
        )

        self.save_lighting_state()

    def apply_builtin_lighting_preset(self, name):
        presets = {
            "Nitro Red": {
                "colors": [
                    "#ff0000",
                    "#ff0000",
                    "#ff0000",
                    "#ff0000",
                ],
                "effect": "static",
                "speed": 50,
            },
            "Ice Blue": {
                "colors": [
                    "#00a8ff",
                    "#42c8ff",
                    "#8ce9ff",
                    "#00a8ff",
                ],
                "effect": "breathing",
                "speed": 45,
            },
            "Cyber": {
                "colors": [
                    "#00ffff",
                    "#7a00ff",
                    "#ff0080",
                    "#00ffff",
                ],
                "effect": "wave",
                "speed": 65,
            },
            "Purple": {
                "colors": [
                    "#6e00ff",
                    "#9b4dff",
                    "#c084ff",
                    "#6e00ff",
                ],
                "effect": "breathing",
                "speed": 40,
            },
        }

        preset = presets.get(name)
        if not preset:
            return

        self.lighting_state.update(
            preset
        )

        self.save_lighting_state()
        self.apply_lighting_state_to_ui()

    def current_lighting_snapshot(self):
        return {
            "colors": list(
                self.lighting_state.get(
                    "colors",
                    [],
                )
            ),
            "effect": self.lighting_state.get(
                "effect",
                "static",
            ),
            "speed": int(
                self.lighting_state.get(
                    "speed",
                    50,
                )
            ),
            "brightness": int(
                self.keyboard_slider.value()
            )
            if hasattr(
                self,
                "keyboard_slider",
            )
            else int(
                self.lighting_state.get(
                    "brightness",
                    100,
                )
            ),
        }

    def save_lighting_preset(self):
        name = (
            self.lighting_preset_name.text()
            .strip()
        )

        if not name:
            return

        presets = dict(
            self.lighting_state.get(
                "presets",
                {},
            )
        )

        presets[name] = (
            self.current_lighting_snapshot()
        )

        self.lighting_state[
            "presets"
        ] = presets

        self.save_lighting_state()
        self.refresh_lighting_preset_combo()

        index = (
            self.lighting_preset_combo.findData(
                name
            )
        )

        if index >= 0:
            self.lighting_preset_combo.setCurrentIndex(
                index
            )

        self.lighting_preset_name.clear()

        self.status_bar.showMessage(
            f"Lighting preset '{name}' saved.",
            3000,
        )

    def apply_saved_lighting_preset(self):
        name = (
            self.lighting_preset_combo.currentData()
        )

        if not name:
            return

        preset = (
            self.lighting_state.get(
                "presets",
                {},
            ).get(name)
        )

        if not isinstance(
            preset,
            dict,
        ):
            return

        existing_presets = (
            self.lighting_state.get(
                "presets",
                {},
            )
        )

        self.lighting_state = {
            **self.lighting_state,
            **preset,
            "presets": existing_presets,
        }

        self.save_lighting_state()
        self.apply_lighting_state_to_ui()

        brightness = int(
            self.lighting_state.get(
                "brightness",
                100,
            )
        )

        if (
            hasattr(
                self,
                "keyboard_slider",
            )
            and self.keyboard_slider.isEnabled()
        ):
            try:
                request({
                    "cmd": "set_keyboard_brightness",
                    "percent": brightness,
                })
            except KNCError:
                pass

    def delete_saved_lighting_preset(self):
        name = (
            self.lighting_preset_combo.currentData()
        )

        if not name:
            return

        presets = dict(
            self.lighting_state.get(
                "presets",
                {},
            )
        )

        presets.pop(
            name,
            None,
        )

        self.lighting_state[
            "presets"
        ] = presets

        self.save_lighting_state()
        self.refresh_lighting_preset_combo()

    def default_ui_settings(self):
        return {
            "osd_enabled": True,
            "close_to_tray": True,
            "hotkey_double_action": "toggle_gaming",
            "cpu_warning_c": 85,
            "gpu_warning_c": 82,
            "sensor_visibility": {
                "cpu": True,
                "gpu": True,
                "cpu_fan": True,
                "gpu_fan": True,
                "gpu_power": True,
                "vram": True,
                "cpu_clock": True,
                "gpu_clock": True,
                "battery": True,
            },
        }

    def load_ui_settings(self):
        defaults = self.default_ui_settings()

        try:
            value = json.loads(
                self.ui_settings_file.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(
                value,
                dict,
            ):
                return defaults

            result = {
                **defaults,
                **value,
            }

            visibility = {
                **defaults["sensor_visibility"],
                **value.get(
                    "sensor_visibility",
                    {},
                ),
            }
            result["sensor_visibility"] = visibility
            return result

        except Exception:
            return defaults

    def save_ui_settings(self):
        tmp = self.ui_settings_file.with_suffix(
            ".tmp"
        )

        tmp.write_text(
            json.dumps(
                self.ui_settings,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        tmp.replace(
            self.ui_settings_file
        )

    def apply_ui_settings(self):
        settings = self.ui_settings

        if hasattr(
            self,
            "osd_checkbox",
        ):
            self.osd_checkbox.setChecked(
                bool(
                    settings.get(
                        "osd_enabled",
                        True,
                    )
                )
            )

            self.close_tray_checkbox.setChecked(
                bool(
                    settings.get(
                        "close_to_tray",
                        True,
                    )
                )
            )

            index = self.hotkey_action_combo.findData(
                settings.get(
                    "hotkey_double_action",
                    "toggle_gaming",
                )
            )

            if index >= 0:
                self.hotkey_action_combo.setCurrentIndex(
                    index
                )

            self.cpu_warning_spin.setValue(
                int(
                    settings.get(
                        "cpu_warning_c",
                        85,
                    )
                )
            )

            self.gpu_warning_spin.setValue(
                int(
                    settings.get(
                        "gpu_warning_c",
                        82,
                    )
                )
            )

            visibility = settings.get(
                "sensor_visibility",
                {},
            )

            for key, checkbox in (
                self.sensor_visibility_checks.items()
            ):
                checkbox.setChecked(
                    bool(
                        visibility.get(
                            key,
                            True,
                        )
                    )
                )

        self.apply_sensor_visibility()

    def apply_sensor_visibility(self):
        if not hasattr(
            self,
            "sensor_cards",
        ):
            return

        visibility = self.ui_settings.get(
            "sensor_visibility",
            {},
        )

        for key, card in (
            self.sensor_cards.items()
        ):
            card.setVisible(
                bool(
                    visibility.get(
                        key,
                        True,
                    )
                )
            )

    def save_ui_settings_from_widgets(self):
        visibility = {}

        for key, checkbox in (
            self.sensor_visibility_checks.items()
        ):
            visibility[key] = (
                checkbox.isChecked()
            )

        self.ui_settings = {
            **self.ui_settings,
            "osd_enabled": (
                self.osd_checkbox.isChecked()
            ),
            "close_to_tray": (
                self.close_tray_checkbox.isChecked()
            ),
            "hotkey_double_action": (
                self.hotkey_action_combo.currentData()
            ),
            "cpu_warning_c": (
                self.cpu_warning_spin.value()
            ),
            "gpu_warning_c": (
                self.gpu_warning_spin.value()
            ),
            "sensor_visibility": visibility,
        }

        self.save_ui_settings()
        self.apply_ui_settings()

        self.status_bar.showMessage(
            "UI settings saved.",
            3500,
        )

    def osd_enabled(self):
        return bool(
            self.ui_settings.get(
                "osd_enabled",
                True,
            )
        )

    def export_command(self, kind):
        try:
            result = subprocess.run(
                [
                    "knc-export",
                    kind,
                ],
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
            )

            if result.returncode != 0:
                self.show_error(
                    result.stderr
                    or result.stdout
                    or "Export failed."
                )
                return None

            first_line = (
                result.stdout.strip().splitlines()[0]
                if result.stdout.strip()
                else ""
            )

            return first_line or None

        except Exception as exc:
            self.show_error(str(exc))
            return None

    def export_support_package(self):
        path = self.export_command(
            "support"
        )

        if path:
            QMessageBox.information(
                self,
                "Kaevryx Nitro Control",
                "Support package created:\n\n"
                + path,
            )

    def export_telemetry_csv(self):
        path = self.export_command(
            "telemetry"
        )

        if path:
            QMessageBox.information(
                self,
                "Kaevryx Nitro Control",
                "Telemetry CSV created:\n\n"
                + path,
            )

    def reports_folder(self):
        return (
            Path.home()
            / "Documents"
            / "KaevryxNitro"
            / "Reports"
        )

    def open_reports_folder(self):
        folder = self.reports_folder()
        folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:
            subprocess.Popen(
                [
                    "xdg-open",
                    str(folder),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            self.show_error(str(exc))

    def setup_tray(self):
        self._allow_close = False
        self.tray = QSystemTrayIcon(self)

        icon = (
            QIcon(ICON_PATH)
            if Path(ICON_PATH).exists()
            else self.windowIcon()
        )
        self.tray.setIcon(icon)
        self.tray.setToolTip("Kaevryx Nitro Control")

        menu = QMenu()

        show_action = QAction("Open Nitro Control", self)
        show_action.triggered.connect(self.show_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()

        for label, preset in [
            ("Quiet", "quiet"),
            ("Balanced", "balanced"),
            ("Gaming", "gaming"),
            ("Max", "max"),
        ]:
            action = QAction(label, self)
            action.triggered.connect(
                lambda _checked=False, p=preset: self.apply_preset(p)
            )
            menu.addAction(action)

        menu.addSeparator()

        fan_auto = QAction("Fans: Auto", self)
        fan_auto.triggered.connect(
            lambda: self.set_fan_mode("auto")
        )
        fan_max = QAction("Fans: Max", self)
        fan_max.triggered.connect(
            lambda: self.set_fan_mode("turbo")
        )
        menu.addAction(fan_auto)
        menu.addAction(fan_max)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self):
        self._allow_close = True
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        close_to_tray = bool(
            self.ui_settings.get(
                "close_to_tray",
                True,
            )
        )

        if (
            close_to_tray
            and hasattr(self, "tray")
            and self.tray.isVisible()
            and not self._allow_close
        ):
            event.ignore()
            self.hide()

            self.tray.showMessage(
                "Kaevryx Nitro Control",
                "KNC continua ativo na bandeja.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
            return

        event.accept()

    def curve_series_changed(self):
        self.curve_editor.set_active_series(
            self.curve_series.currentData()
        )

    def save_fan_curves(self):
        try:
            cpu, gpu = self.curve_editor.curves()
            request({
                "cmd": "set_fan_curves",
                "cpu_points": cpu,
                "gpu_points": gpu,
            })
            self.status_bar.showMessage(
                "Curvas CPU/GPU salvas.", 3500
            )
        except KNCError as exc:
            self.show_error(str(exc))

    def reset_fan_curves(self):
        try:
            data = request({"cmd": "reset_fan_curves"})
            self.curve_editor.set_curves(
                data.get("fan_curve_cpu"),
                data.get("fan_curve_gpu"),
            )
            self.status_bar.showMessage(
                "Curvas restauradas para o padrão.", 3500
            )
        except KNCError as exc:
            self.show_error(str(exc))

    def load_user_profiles(self):
        try:
            value = json.loads(
                self.profiles_file.read_text(encoding="utf-8")
            )
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def save_user_profiles(self):
        tmp = self.profiles_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                self.user_profiles,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.profiles_file)

    def refresh_user_profiles(self):
        if not hasattr(self, "user_profile_list"):
            return
        self.user_profile_list.clear()
        for name in sorted(self.user_profiles):
            self.user_profile_list.addItem(name)

    def save_current_user_profile(self):
        if not self._last_status:
            return

        name, ok = QInputDialog.getText(
            self,
            "Save profile",
            "Profile name:",
        )
        name = name.strip()

        if not ok or not name:
            return

        status = self._last_status
        self.user_profiles[name] = {
            "profile": status.get("profile", "balanced"),
            "profile_owner": status.get("profile_owner", "acer"),
            "fan1_mode": status.get("fan1_mode", "auto"),
            "fan2_mode": status.get("fan2_mode", "auto"),
            "fan1_percent": status.get("fan1_percent", 50),
            "fan2_percent": status.get("fan2_percent", 50),
            "smart_curve_enabled": bool(
                status.get("smart_curve_enabled", False)
            ),
            "fan_curve_cpu": status.get("fan_curve_cpu", []),
            "fan_curve_gpu": status.get("fan_curve_gpu", []),
            "lighting": self.current_lighting_snapshot(),
        }
        self.save_user_profiles()
        self.refresh_user_profiles()
        self.status_bar.showMessage(
            f"Profile '{name}' saved.", 3500
        )

    def selected_user_profile_name(self):
        item = self.user_profile_list.currentItem()
        return item.text() if item else None

    def apply_selected_user_profile(self):
        name = self.selected_user_profile_name()
        if not name:
            return

        profile = self.user_profiles.get(name)
        if not profile:
            return

        try:
            request({
                "cmd": "set_owner",
                "owner": profile.get("profile_owner", "acer"),
            })
            request({
                "cmd": "set_fan_curves",
                "cpu_points": profile.get("fan_curve_cpu", []),
                "gpu_points": profile.get("fan_curve_gpu", []),
            })
            request({
                "cmd": "set_profile",
                "profile": profile.get("profile", "balanced"),
            })

            if profile.get("smart_curve_enabled"):
                request({
                    "cmd": "set_smart_curve",
                    "enabled": True,
                })
            elif (
                profile.get("fan1_mode") == "manual"
                or profile.get("fan2_mode") == "manual"
            ):
                request({
                    "cmd": "set_manual_pair",
                    "fan1_percent": profile.get("fan1_percent", 50),
                    "fan2_percent": profile.get("fan2_percent", 50),
                })
            else:
                request({
                    "cmd": "set_fan",
                    "fan": "both",
                    "mode": profile.get("fan1_mode", "auto"),
                })

            lighting = profile.get(
                "lighting"
            )

            if isinstance(
                lighting,
                dict,
            ):
                presets = self.lighting_state.get(
                    "presets",
                    {},
                )

                self.lighting_state = {
                    **self.lighting_state,
                    **lighting,
                    "presets": presets,
                }

                self.save_lighting_state()
                self.apply_lighting_state_to_ui()

                brightness = lighting.get(
                    "brightness"
                )

                if (
                    brightness is not None
                    and self.keyboard_slider.isEnabled()
                ):
                    try:
                        request({
                            "cmd": "set_keyboard_brightness",
                            "percent": int(brightness),
                        })
                    except KNCError:
                        pass

            self.status_bar.showMessage(
                f"Profile '{name}' applied.", 3500
            )
            self.refresh()

        except KNCError as exc:
            self.show_error(str(exc))

    def delete_selected_user_profile(self):
        name = self.selected_user_profile_name()
        if not name:
            return
        self.user_profiles.pop(name, None)
        self.save_user_profiles()
        self.refresh_user_profiles()

    def save_power_automation(self):
        try:
            request({
                "cmd": "set_power_automation",
                "enabled": self.auto_power_checkbox.isChecked(),
                "ac_preset": self.ac_preset_combo.currentData(),
                "battery_preset": self.battery_preset_combo.currentData(),
            })
            self.status_bar.showMessage(
                "AC/Battery automation updated.", 3500
            )
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def add_app_rule(self):
        process = self.app_process.text().strip()
        if not process:
            return

        preset = self.app_preset_combo.currentData()
        item = QListWidgetItem(f"{process}  →  {preset}")
        item.setData(
            Qt.ItemDataRole.UserRole,
            {"process": process, "preset": preset},
        )
        self.app_rules_list.addItem(item)
        self.app_process.clear()

    def remove_app_rule(self):
        row = self.app_rules_list.currentRow()
        if row >= 0:
            self.app_rules_list.takeItem(row)

    def current_app_rules(self):
        rules = []
        for i in range(self.app_rules_list.count()):
            item = self.app_rules_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                rules.append(data)
        return rules

    def save_app_automation(self):
        try:
            request({
                "cmd": "set_app_automation",
                "enabled": self.app_auto_checkbox.isChecked(),
                "rules": self.current_app_rules(),
                "fallback_preset": self.app_fallback_combo.currentData(),
            })
            self.status_bar.showMessage(
                "Application automation updated.", 3500
            )
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def apply_preset(self, name):
        try:
            request({"cmd": "set_preset", "name": name})
            self.show_osd(
                name.upper(),
                "Operating mode changed",
            )
            self.status_bar.showMessage(
                f"Preset {name.upper()} aplicado.", 3500
            )
            self.refresh()
            self._osd_last_profile = (
                self._last_status.get("profile")
                if self._last_status
                else self._osd_last_profile
            )
        except KNCError as exc:
            self.show_error(str(exc))

    def owner_changed(self):
        if self._syncing:
            return
        try:
            request({
                "cmd": "set_owner",
                "owner": self.owner.currentData(),
            })
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
                    "Este perfil não existe na API padrão do KDE.\n"
                    "Ativar controle Acer completo?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

                index = self.owner.findData("acer")
                self._syncing = True
                self.owner.setCurrentIndex(index)
                self._syncing = False
                request({"cmd": "set_owner", "owner": "acer"})

            request({"cmd": "set_profile", "profile": profile})
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def set_fan_mode(self, mode):
        try:
            request({"cmd": "set_smart_curve", "enabled": False})
            if mode == "manual":
                self.apply_manual()
            else:
                request({
                    "cmd": "set_fan",
                    "fan": "both",
                    "mode": mode,
                })
            self.show_osd(
                "FAN MODE",
                mode.upper(),
            )
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def apply_manual(self):
        try:
            request({"cmd": "set_smart_curve", "enabled": False})
            request({
                "cmd": "set_manual_pair",
                "fan1_percent": self.cpu_slider.value(),
                "fan2_percent": self.gpu_slider.value(),
            })
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def smart_curve_changed(self, checked):
        if self._syncing:
            return
        try:
            request({
                "cmd": "set_smart_curve",
                "enabled": bool(checked),
            })
            self.refresh()
        except KNCError as exc:
            self.show_error(str(exc))

    def apply_keyboard_brightness(self):
        brightness = int(
            self.keyboard_slider.value()
        )

        self.lighting_state[
            "brightness"
        ] = brightness
        self.save_lighting_state()

        try:
            request({
                "cmd": "set_keyboard_brightness",
                "percent": brightness,
            })
            self.refresh()

        except KNCError as exc:
            self.show_error(str(exc))

    def run_doctor(self):
        try:
            subprocess.Popen(
                ["konsole", "-e", "bash", "-lc", "knc-doctor; echo; read -p 'ENTER para fechar...'"],
                start_new_session=True,
            )
        except Exception as exc:
            self.show_error(str(exc))

    def test_hotkey(self):
        try:
            subprocess.Popen(
                ["knc-hotkey-test"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.status_bar.showMessage(
                "Sinal de teste enviado ao bridge Nitro.", 3500
            )
        except Exception as exc:
            self.show_error(str(exc))

    def refresh(self):
        try:
            data = request({"cmd": "status"})
            self._last_status = data
            self.update_status(data)
        except KNCError as exc:
            self.status_bar.showMessage(str(exc))

    def update_status(self, data):
        self._syncing = True
        try:
            acer = data.get("acer", {})
            cpu_temp_mC = data.get("cpu_temp_mC")
            gpu_temp_mC = data.get("gpu_temp_mC")
            cpu_temp = (
                cpu_temp_mC / 1000 if cpu_temp_mC is not None else None
            )
            gpu_temp = (
                gpu_temp_mC / 1000 if gpu_temp_mC is not None else None
            )

            cpu_util = psutil.cpu_percent(interval=None)
            gpu = nvidia_metrics()
            gpu_util = gpu["util"] if gpu else 0

            self.cpu_card.value.setText(
                f"{int(cpu_temp)}°C" if cpu_temp is not None else "—"
            )
            self.cpu_card.detail.setText(f"LOAD {cpu_util:.0f}%")

            cpu_freq = psutil.cpu_freq()
            if (
                cpu_freq is not None
                and cpu_freq.current
            ):
                self.cpu_clock_card.value.setText(
                    f"{cpu_freq.current / 1000:.2f} GHz"
                )
                self.cpu_clock_card.detail.setText(
                    "CURRENT"
                )
            else:
                self.cpu_clock_card.value.setText("—")
                self.cpu_clock_card.detail.setText("")

            battery = psutil.sensors_battery()
            if battery is not None:
                self.battery_card.value.setText(
                    f"{battery.percent:.0f}%"
                )
                if battery.power_plugged:
                    self.battery_card.detail.setText(
                        "AC / CHARGING"
                    )
                else:
                    self.battery_card.detail.setText(
                        "BATTERY"
                    )
            else:
                self.battery_card.value.setText("—")
                self.battery_card.detail.setText("")

            if gpu:
                self.gpu_card.value.setText(f"{gpu['temp']}°C")
                self.gpu_card.detail.setText(f"LOAD {gpu['util']}%")
                self.power_card.value.setText(f"{gpu['power']:.1f}W")
                self.power_card.detail.setText("NVIDIA")
                self.vram_card.value.setText(
                    f"{gpu['vram_used']} MB"
                )
                self.vram_card.detail.setText(
                    f"OF {gpu['vram_total']} MB"
                )
                self.gpu_clock_card.value.setText(
                    f"{gpu['clock_gpu']} MHz"
                )
                self.gpu_clock_card.detail.setText(
                    f"MEM {gpu['clock_mem']} MHz"
                )
            else:
                self.gpu_card.value.setText("—")
                self.power_card.value.setText("—")
                self.vram_card.value.setText("—")
                self.gpu_clock_card.value.setText("—")
                self.gpu_clock_card.detail.setText("")

            cpu_warning = int(
                self.ui_settings.get(
                    "cpu_warning_c",
                    85,
                )
            )
            gpu_warning = int(
                self.ui_settings.get(
                    "gpu_warning_c",
                    82,
                )
            )

            self.cpu_card.set_alert(
                cpu_temp is not None
                and cpu_temp >= cpu_warning
            )

            gpu_alert_temp = (
                gpu["temp"]
                if gpu
                else gpu_temp
            )
            self.gpu_card.set_alert(
                gpu_alert_temp is not None
                and gpu_alert_temp >= gpu_warning
            )

            fan1 = acer.get("fan1_input") or 0
            fan2 = acer.get("fan2_input") or 0

            self.cpu_fan_card.value.setText(fmt_rpm(fan1))
            self.cpu_fan_card.detail.setText("RPM")
            self.gpu_fan_card.value.setText(fmt_rpm(fan2))
            self.gpu_fan_card.detail.setText("RPM")

            f1_pct = int(data.get("fan1_percent", 50))
            f2_pct = int(data.get("fan2_percent", 50))
            self.cpu_dial.set_data(fan1, f1_pct)
            self.gpu_dial.set_data(fan2, f2_pct)

            if data.get("fan1_mode") == "manual":
                self.cpu_slider.setValue(f1_pct)
            if data.get("fan2_mode") == "manual":
                self.gpu_slider.setValue(f2_pct)

            same = (
                data.get("fan1_mode")
                if data.get("fan1_mode") == data.get("fan2_mode")
                else None
            )
            self.fan_auto.setChecked(same == "auto")
            self.fan_custom.setChecked(same == "manual")
            self.fan_max.setChecked(same == "turbo")

            self.smart_curve.setChecked(
                bool(data.get("smart_curve_enabled"))
            )

            profile = data.get("profile", "unknown")
            self.quick_profile.setText(profile.upper())
            for key, button in self.profile_buttons.items():
                button.setChecked(key == profile)

            owner = data.get("profile_owner", "kde")
            owner_index = self.owner.findData(owner)
            if owner_index >= 0:
                self.owner.setCurrentIndex(owner_index)

            keyboard = data.get("keyboard", {})
            if keyboard.get("available"):
                percent = keyboard.get("percent")
                if percent is not None:
                    self.keyboard_slider.setValue(
                        int(percent)
                    )
                    self.lighting_state[
                        "brightness"
                    ] = int(percent)

                self.keyboard_slider.setEnabled(True)
                self.keyboard_status.setText(
                    "BRIGHTNESS BACKEND: ACTIVE  •  "
                    f"{keyboard.get('path')}"
                )
            else:
                self.keyboard_slider.setEnabled(False)
                self.keyboard_status.setText(
                    "BRIGHTNESS BACKEND: NOT EXPOSED BY KERNEL"
                )

            self.zone_preview.set_temperatures(
                cpu_temp,
                gpu["temp"] if gpu else gpu_temp,
            )

            self.curve_editor.set_curves(
                data.get("fan_curve_cpu"),
                data.get("fan_curve_gpu"),
            )

            self.auto_power_checkbox.setChecked(
                bool(data.get("auto_power_enabled"))
            )
            for combo, value in (
                (
                    self.ac_preset_combo,
                    data.get("auto_power_ac_preset", "balanced"),
                ),
                (
                    self.battery_preset_combo,
                    data.get("auto_power_battery_preset", "quiet"),
                ),
                (
                    self.app_fallback_combo,
                    data.get("app_fallback_preset", "balanced"),
                ),
            ):
                idx = combo.findData(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

            self.app_auto_checkbox.setChecked(
                bool(data.get("app_automation_enabled"))
            )

            backend_rules = data.get("app_rules", [])
            current_rules = self.current_app_rules()
            if current_rules != backend_rules:
                self.app_rules_list.clear()
                for rule in backend_rules:
                    item = QListWidgetItem(
                        f"{rule.get('process')}  →  {rule.get('preset')}"
                    )
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        {
                            "process": rule.get("process"),
                            "preset": rule.get("preset"),
                        },
                    )
                    self.app_rules_list.addItem(item)

            boost_active = bool(
                data.get("boost_active")
            )
            boost_remaining = int(
                data.get("boost_remaining_s", 0)
            )

            self.boost_status.setText(
                f"{boost_remaining}s"
                if boost_active
                else "IDLE"
            )
            self.boost_cancel.setEnabled(
                boost_active
            )

            profile_now = data.get(
                "profile",
                "unknown",
            )

            if self._osd_last_profile is None:
                self._osd_last_profile = profile_now

            elif (
                profile_now
                != self._osd_last_profile
            ):
                self._osd_last_profile = profile_now
                self.show_osd(
                    profile_now.upper(),
                    "Profile changed",
                )

            if (
                not boost_active
                and self._osd_last_boost
            ):
                self._osd_last_boost = False
                self.show_osd(
                    "FAN BOOST",
                    "Returned to normal control",
                )

            elif boost_active:
                self._osd_last_boost = True

            self.backend_info.setText(
                "Daemon: online\n"
                f"Profile owner: {owner}\n"
                f"Platform profile: {profile}\n"
                f"Acer hwmon: {data.get('acer_hwmon')}\n"
                f"Smart curve: {data.get('smart_curve_enabled')}\n"
                f"Thermal safety: {data.get('safety_override')}\n"
                f"Power automation: {data.get('auto_power_enabled')}\n"
                f"AC online: {data.get('ac_online')}\n"
                f"App automation: {data.get('app_automation_enabled')}\n"
                f"Active app rule: {data.get('active_app_rule')}\n"
                f"Fan boost: {data.get('boost_active')} "
                f"({data.get('boost_remaining_s')}s)\n"
                "Telemetry: "
                "/var/lib/kaevryx-nitro-control/telemetry.jsonl\n"
                f"Keyboard backend: "
                f"{keyboard.get('path') or 'not available'}"
            )

            if data.get("safety_override"):
                self.status_bar.showMessage(
                    data.get("last_error", "Proteção térmica ativa.")
                )
            elif data.get("last_error"):
                self.status_bar.showMessage(
                    data.get("last_error"), 6000
                )

        finally:
            self._syncing = False


def notify_running_instance():
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
    server.listen(SINGLE_INSTANCE)

    window = MainWindow()

    if (
        "--tray" in sys.argv
        and window.tray.isVisible()
    ):
        window.hide()
    else:
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
