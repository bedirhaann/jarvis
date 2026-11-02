import math
import os
import sys
import time

# Ensure project root is on sys.path when ui/orb.py is executed directly
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from ui.chat import ChatWindow


_TWO_PI = math.pi * 2.0


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    return QColor(
        int(_lerp(c1.red(), c2.red(), t)),
        int(_lerp(c1.green(), c2.green(), t)),
        int(_lerp(c1.blue(), c2.blue(), t)),
        int(_lerp(c1.alpha(), c2.alpha(), t)),
    )


class JarvisOrb(QWidget):
    """Holographic 3D-styled JARVIS energy sphere with multi-axis elliptical orbitals,
    floating energy particles, state-driven color/wave dynamics, Wayland-compatible
    drag/click handling, and right-click context menu."""

    state_updated = Signal(str)
    shutdown_timeout_occurred = Signal(str)
    shutdown_completed = Signal()

    VALID_STATES = {"connecting", "idle", "thinking", "responding", "error"}
    DRAG_THRESHOLD = 6
    ORB_WINDOW_SIZE = 432
    ORB_SCALE = 3.00
    BASE_SPHERE_RADIUS = 30.5

    # State visual profiles:
    # (core_color, mid_color, outer_color, accent_color, phase_speed, pulse_amp, orbit_mult)
    STATE_PROFILES = {
        "connecting": (
            QColor(255, 248, 220, 255),
            QColor(245, 158, 11, 230),
            QColor(180, 83, 9, 105),
            QColor(251, 191, 36, 220),
            0.055,
            0.065,
            0.85,
        ),
        "idle": (
            QColor(224, 252, 255, 255),
            QColor(0, 200, 255, 225),
            QColor(2, 84, 199, 95),
            QColor(56, 189, 248, 215),
            0.042,
            0.055,
            1.0,
        ),
        "thinking": (
            QColor(243, 232, 255, 255),
            QColor(168, 85, 247, 235),
            QColor(79, 70, 229, 115),
            QColor(56, 189, 248, 235),
            0.092,
            0.095,
            1.85,
        ),
        "responding": (
            QColor(236, 254, 255, 255),
            QColor(34, 211, 238, 242),
            QColor(2, 132, 199, 125),
            QColor(125, 211, 252, 245),
            0.078,
            0.085,
            1.55,
        ),
        "error": (
            QColor(254, 226, 226, 255),
            QColor(239, 68, 68, 230),
            QColor(153, 27, 27, 110),
            QColor(248, 113, 113, 215),
            0.065,
            0.080,
            0.90,
        ),
    }

    # 3 elliptical orbital definitions: (base_tilt_deg, aspect_ratio_ry_rx, speed_factor, arc_span_deg)
    ORBITAL_SPECS = (
        (-28.0, 0.44, 1.00, 95),
        (36.0, 0.38, -1.32, 110),
        (76.0, 0.34, 0.78, 80),
    )

    # Asynchronous shutdown coordination parameters:
    # - Poll interval (ms) checks thread completion without blocking the GUI thread.
    # - Default timeout (ms) covers AntigravityClient's full staged close window
    #   (5s grace + 3s terminate + 1s kill = 9s, plus 1s margin = 10000 ms).
    QUIT_POLL_INTERVAL_MS = 50
    QUIT_FALLBACK_TIMEOUT_MS = 10000

    def __init__(
        self,
        start_worker: bool = True,
        chat_window: ChatWindow | None = None,
        app_context=None,
    ):
        super().__init__()
        self.app_context = app_context

        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        self.setWindowTitle("JARVIS")
        self.setFixedSize(self.ORB_WINDOW_SIZE, self.ORB_WINDOW_SIZE)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.phase = 0.0
        self.ring_angle = 0.0
        self.orbit_angles = [15.0, 140.0, 260.0]
        self._wave_progress = 0.0
        self._state = "connecting"
        self._token_energy = 0.0
        self._quitting = False
        self._quit_completed = False
        self._shutdown_state = "idle"  # "idle" | "stopping" | "timed_out" | "completed"
        self._shutdown_started_at: float | None = None
        self._shutdown_error: str | None = None
        self.shutdown_timeout_ms: int = self.QUIT_FALLBACK_TIMEOUT_MS

        # Smoothly interpolated current colors
        init_core, init_mid, init_outer, init_accent, _, _, _ = self.STATE_PROFILES["connecting"]
        self._cur_core = QColor(init_core)
        self._cur_mid = QColor(init_mid)
        self._cur_outer = QColor(init_outer)
        self._cur_accent = QColor(init_accent)
        self._cur_orbit_mult = 0.85

        # Pre-allocated holographic energy particles (zero per-frame allocations)
        self._particles = self._init_particles(count=14)

        # Drag / click interaction state
        self._press_global_pos: QPoint | None = None
        self._press_window_pos: QPoint | None = None
        self._is_dragging = False

        # Chat window wiring
        self.chat_window = (
            chat_window
            if chat_window is not None
            else ChatWindow(start_worker=start_worker, app_context=app_context)
        )
        self.chat_window.state_changed.connect(self.set_state)
        self.chat_window.token_streamed.connect(self.on_token_streamed)
        self.chat_window.shutdown_finished.connect(self._on_chat_shutdown_finished)

        # Context menu setup
        self.context_menu = self._build_context_menu()

        # Non-blocking periodic shutdown monitor timer
        self._shutdown_poll_timer = QTimer(self)
        self._shutdown_poll_timer.setInterval(self.QUIT_POLL_INTERVAL_MS)
        self._shutdown_poll_timer.timeout.connect(self._on_shutdown_poll_tick)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate)
        self.timer.start(30)

        self.move_to_corner()

    @staticmethod
    def _init_particles(count: int = 14) -> list[dict[str, float]]:
        """Create deterministic, evenly distributed orbital energy particles."""
        particles: list[dict[str, float]] = []
        for i in range(count):
            ratio = i / float(count)
            direction = 1.0 if i % 2 == 0 else -1.0
            particles.append({
                "angle": ratio * _TWO_PI,
                "speed": direction * (0.018 + (i % 5) * 0.0045),
                "dist": 0.62 + (i % 4) * 0.14,
                "tilt": (i * 37.0) % 180.0,
                "wobble_phase": ratio * _TWO_PI * 2.0,
                "size": 1.35 + (i % 3) * 0.45,
                "alpha": 0.55 + (i % 4) * 0.13,
            })
        return particles

    # ------------------------------------------------------------------
    # State & Token Feedback
    # ------------------------------------------------------------------
    def state(self) -> str:
        return self._state

    def token_energy(self) -> float:
        return self._token_energy

    def orbital_angles(self) -> list[float]:
        return list(self.orbit_angles)

    def particles(self) -> list[dict[str, float]]:
        return self._particles

    def visible_sphere_diameter(self) -> float:
        """Return the nominal visible diameter (in px) of the holographic energy sphere."""
        return 2.0 * self.BASE_SPHERE_RADIUS * self.ORB_SCALE * 1.68

    @Slot(str)
    def set_state(self, new_state: str):
        normalized = (new_state or "").strip().lower()
        if normalized not in self.VALID_STATES:
            normalized = "idle"
        if self._state != normalized:
            self._state = normalized
            self.state_updated.emit(self._state)
        self.update()

    @Slot()
    def on_token_streamed(self):
        if self._state != "responding":
            self._state = "responding"
            self.state_updated.emit(self._state)
        self._token_energy = min(1.0, self._token_energy + 0.38)
        self.update()

    # ------------------------------------------------------------------
    # Positioning & Window Controls
    # ------------------------------------------------------------------
    def move_to_corner(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(40, 40)
            return
        geom = screen.availableGeometry()
        margin_x = 20
        margin_y = 40
        x = geom.x() + geom.width() - self.width() - margin_x
        y = geom.y() + geom.height() - self.height() - margin_y
        max_x = geom.x() + max(0, geom.width() - self.width() - 8)
        max_y = geom.y() + max(0, geom.height() - self.height() - 8)
        self.move(
            max(geom.x() + 8, min(x, max_x)),
            max(geom.y() + 8, min(y, max_y)),
        )

    def _position_chat_near_orb(self):
        """Position ChatWindow conveniently near the orb on first show while keeping
        it strictly inside the screen's available geometry."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        orb_geom = self.frameGeometry()

        # Prefer placing ChatWindow to the left of the large 432x432 Orb if space allows
        target_x = orb_geom.left() - self.chat_window.width() - 14
        if target_x < geom.left() + 10:
            target_x = orb_geom.right() - self.chat_window.width()

        target_y = orb_geom.bottom() - self.chat_window.height()
        if target_y < geom.top() + 10:
            target_y = geom.top() + 10

        target_x = max(
            geom.left() + 10,
            min(target_x, geom.right() - self.chat_window.width() - 10),
        )
        target_y = max(
            geom.top() + 10,
            min(target_y, geom.bottom() - self.chat_window.height() - 10),
        )
        self.chat_window.move(target_x, target_y)

    def shutdown_state(self) -> str:
        return self._shutdown_state

    def shutdown_error(self) -> str | None:
        return self._shutdown_error

    @Slot()
    def toggle_chat_window(self):
        if self._quitting:
            return
        if self.chat_window.isVisible() and not self.chat_window.isMinimized():
            self.chat_window.hide()
        else:
            if not self.chat_window.isVisible():
                self._position_chat_near_orb()
            self.chat_window.show()
            self.chat_window.raise_()
            self.chat_window.activateWindow()
        self._update_menu_labels()

    @Slot()
    def clear_chat(self):
        if self._quitting:
            return
        self.chat_window.clear_messages()

    @Slot()
    def quit_application(self):
        """Initiate asynchronous, non-blocking shutdown of worker and close application
        strictly after worker_thread and monitor_thread have genuinely exited."""
        if self._quitting:
            return
        self._quitting = True
        self._quit_completed = False
        self._shutdown_state = "stopping"
        self._shutdown_started_at = time.monotonic()
        self._shutdown_error = None

        self.timer.stop()
        for act_name in (
            "action_toggle_chat",
            "action_clear_chat",
            "action_reset_corner",
            "action_quit",
        ):
            act = getattr(self, act_name, None)
            if act is not None:
                act.setEnabled(False)

        self.chat_window.hide()
        self.hide()

        self.chat_window.request_shutdown()
        if not self._try_complete_quit():
            self._shutdown_poll_timer.start()

    @Slot()
    def _on_chat_shutdown_finished(self):
        if self._quitting:
            self._try_complete_quit()

    @Slot()
    def _on_shutdown_poll_tick(self):
        if not self._quitting or self._quit_completed:
            self._shutdown_poll_timer.stop()
            return

        if self._try_complete_quit():
            return

        started = self._shutdown_started_at or time.monotonic()
        elapsed_ms = (time.monotonic() - started) * 1000.0

        if elapsed_ms >= self.shutdown_timeout_ms and self._shutdown_state == "stopping":
            self._shutdown_state = "timed_out"
            self._shutdown_error = (
                f"JARVIS shutdown timed out after {int(elapsed_ms)} ms; "
                "escalating worker abort and awaiting QThread termination."
            )
            sys.stderr.write(self._shutdown_error + "\n")
            self.shutdown_timeout_occurred.emit(self._shutdown_error)
            self.chat_window.escalate_shutdown()
            # Re-check immediately in case escalation unblocked the thread
            self._try_complete_quit()
        elif self._shutdown_state == "timed_out":
            self.chat_window.escalate_shutdown()
            self._try_complete_quit()

    def _try_complete_quit(self) -> bool:
        """Verify via QThread.wait() return values that all background threads have
        stopped before transitioning to 'completed' and calling app.quit()."""
        if self._quit_completed:
            return True
        if not self.chat_window.verify_threads_stopped(wait_ms=20):
            return False

        self._quit_completed = True
        self._shutdown_state = "completed"
        self._shutdown_poll_timer.stop()
        self.shutdown_completed.emit()
        app = QApplication.instance()
        if app is not None:
            app.quit()
        return True

    def _finish_app_quit(self):
        """Backward-compatible helper that delegates to _try_complete_quit()."""
        self._try_complete_quit()

    # ------------------------------------------------------------------
    # Context Menu
    # ------------------------------------------------------------------
    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setObjectName("orbContextMenu")
        menu.setStyleSheet("""
            QMenu {
                background-color: #0c1425;
                border: 1px solid #1e3a5f;
                border-radius: 10px;
                padding: 6px;
                color: #e8f4ff;
                font-family: 'Ubuntu', 'Segoe UI', sans-serif;
                font-size: 12.5px;
            }
            QMenu::item {
                padding: 7px 16px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background-color: #0284c7;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background: #1b2c47;
                margin: 4px 8px;
            }
        """)

        self.action_toggle_chat = QAction("Sohbeti Aç/Gizle", self)
        self.action_toggle_chat.triggered.connect(self.toggle_chat_window)

        self.action_clear_chat = QAction("Sohbeti Temizle", self)
        self.action_clear_chat.triggered.connect(self.clear_chat)

        self.action_reset_corner = QAction("Köşeye Sıfırla", self)
        self.action_reset_corner.triggered.connect(self.move_to_corner)

        self.action_quit = QAction("Çıkış", self)
        self.action_quit.triggered.connect(self.quit_application)

        menu.addAction(self.action_toggle_chat)
        menu.addAction(self.action_clear_chat)
        menu.addAction(self.action_reset_corner)
        menu.addSeparator()
        menu.addAction(self.action_quit)
        return menu

    def _update_menu_labels(self):
        if self.chat_window.isVisible() and not self.chat_window.isMinimized():
            self.action_toggle_chat.setText("Sohbeti Gizle")
        else:
            self.action_toggle_chat.setText("Sohbeti Aç")

    def contextMenuEvent(self, event):
        self._update_menu_labels()
        self.context_menu.popup(event.globalPos())
        event.accept()

    # ------------------------------------------------------------------
    # Animation Loop & Holographic Sphere Rendering
    # ------------------------------------------------------------------
    def animate(self):
        (
            target_core,
            target_mid,
            target_outer,
            target_accent,
            speed,
            _,
            target_orbit_mult,
        ) = self.STATE_PROFILES.get(self._state, self.STATE_PROFILES["idle"])

        # Smoothly interpolate colors & speed multiplier (prevents jarring flashes)
        self._cur_core = _lerp_color(self._cur_core, target_core, 0.14)
        self._cur_mid = _lerp_color(self._cur_mid, target_mid, 0.14)
        self._cur_outer = _lerp_color(self._cur_outer, target_outer, 0.14)
        self._cur_accent = _lerp_color(self._cur_accent, target_accent, 0.14)
        self._cur_orbit_mult = _lerp(self._cur_orbit_mult, target_orbit_mult, 0.12)

        # Advance breathing phase and independent orbital ring angles
        effective_speed = speed + (0.045 * self._token_energy)
        self.phase += effective_speed

        orbit_step = (1.65 + 1.45 * self._token_energy) * self._cur_orbit_mult
        for idx, (_, _, factor, _) in enumerate(self.ORBITAL_SPECS):
            self.orbit_angles[idx] = (self.orbit_angles[idx] + orbit_step * factor) % 360.0
        self.ring_angle = self.orbit_angles[0]

        # Advance outward energy wave for responding / thinking states
        wave_speed = 0.032 * self._cur_orbit_mult + 0.035 * self._token_energy
        self._wave_progress = (self._wave_progress + wave_speed) % 1.0

        # Update floating particles smoothly
        particle_mult = self._cur_orbit_mult + 0.85 * self._token_energy
        for p in self._particles:
            p["angle"] = (p["angle"] + p["speed"] * particle_mult) % _TWO_PI
            p["wobble_phase"] = (p["wobble_phase"] + 0.035 * particle_mult) % _TWO_PI

        # Smooth exponential decay of token energy
        if self._token_energy > 0.001:
            self._token_energy = max(0.0, self._token_energy * 0.88 - 0.008)
        else:
            self._token_energy = 0.0

        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        _, _, _, _, _, base_amp, _ = self.STATE_PROFILES.get(
            self._state, self.STATE_PROFILES["idle"]
        )
        amp = base_amp + 0.055 * self._token_energy
        breath = 1.0 + amp * math.sin(self.phase)

        rect_f = QRectF(self.rect())
        center_f = rect_f.center()
        cx, cy = center_f.x(), center_f.y()
        s = self.ORB_SCALE
        base_r = self.BASE_SPHERE_RADIUS * s * breath

        # 1. Deep Dark Translucent Containment Field (gives high-contrast sci-fi depth)
        backdrop_r = 52.0 * s
        bg_grad = QRadialGradient(center_f, backdrop_r)
        bg_grad.setColorAt(0.0, QColor(4, 9, 22, 215))
        bg_grad.setColorAt(0.58, QColor(5, 12, 28, 165))
        bg_grad.setColorAt(0.84, QColor(6, 15, 34, 70))
        bg_grad.setColorAt(1.0, QColor(4, 9, 22, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_grad)
        painter.drawEllipse(center_f, backdrop_r, backdrop_r)

        # 2. Outer Holographic Ambient Glow
        aura_r = base_r * (1.68 + 0.12 * self._token_energy)
        aura_grad = QRadialGradient(center_f, aura_r)
        c_mid_soft = QColor(
            self._cur_mid.red(),
            self._cur_mid.green(),
            self._cur_mid.blue(),
            int(165 + 45 * self._token_energy),
        )
        c_outer_fade = QColor(
            self._cur_outer.red(),
            self._cur_outer.green(),
            self._cur_outer.blue(),
            0,
        )
        aura_grad.setColorAt(0.0, self._cur_core)
        aura_grad.setColorAt(0.32, c_mid_soft)
        aura_grad.setColorAt(0.68, self._cur_outer)
        aura_grad.setColorAt(1.0, c_outer_fade)
        painter.setBrush(aura_grad)
        painter.drawEllipse(center_f, aura_r, aura_r)

        # 3. Dynamic Expanding Energy Pulse Wave (prominent in responding/thinking/error)
        if self._state in ("responding", "thinking", "error") or self._token_energy > 0.04:
            wave_r = base_r * (0.48 + 0.92 * self._wave_progress)
            wave_alpha = int((1.0 - self._wave_progress) * (95 + 90 * self._token_energy))
            if wave_alpha > 4:
                wave_color = QColor(
                    self._cur_accent.red(),
                    self._cur_accent.green(),
                    self._cur_accent.blue(),
                    wave_alpha,
                )
                wave_pen = QPen(wave_color, 1.4 * s)
                painter.setPen(wave_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(center_f, wave_r, wave_r)

        # 4. Three Inclined Elliptical Orbital Rings (3D Gyroscope Hologram)
        orbit_rx = base_r * (1.26 + 0.05 * self._token_energy)
        for idx, (base_tilt, aspect, _, arc_span) in enumerate(self.ORBITAL_SPECS):
            orbit_ry = orbit_rx * aspect
            precession = 5.0 * math.sin(self.phase * 0.45 + idx * 1.7)
            tilt_deg = base_tilt + precession
            rot_deg = self.orbit_angles[idx]

            painter.save()
            painter.translate(center_f)
            painter.rotate(tilt_deg)

            ellipse_rect = QRectF(-orbit_rx, -orbit_ry, orbit_rx * 2.0, orbit_ry * 2.0)

            # Subtle full elliptical wireframe track
            track_color = QColor(
                self._cur_accent if idx == 1 else self._cur_mid
            )
            track_color.setAlpha(int(52 + 35 * self._token_energy))
            track_pen = QPen(track_color, 1.0 * s)
            painter.setPen(track_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(ellipse_rect)

            # Glowing active energy arc segments along the tilted ellipse
            arc_color = QColor(self._cur_core if idx != 1 else self._cur_accent)
            arc_color.setAlpha(int(175 + 65 * self._token_energy))
            arc_pen = QPen(arc_color, (1.85 if idx == 0 else 1.55) * s)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc_pen)

            start_16 = int(rot_deg * 16)
            span_16 = int((arc_span + 20 * self._token_energy) * 16)
            painter.drawArc(ellipse_rect, start_16, span_16)
            painter.drawArc(ellipse_rect, start_16 + 180 * 16, int(span_16 * 0.65))

            # Orbital energy node riding the leading edge of each ellipse
            node_rad = math.radians(rot_deg)
            nx = orbit_rx * math.cos(node_rad)
            ny = -orbit_ry * math.sin(node_rad)
            node_pt = QPointF(nx, ny)

            node_glow = QColor(self._cur_mid)
            node_glow.setAlpha(135)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(node_glow)
            painter.drawEllipse(node_pt, 3.4 * s, 3.4 * s)

            painter.setBrush(self._cur_core)
            painter.drawEllipse(node_pt, 1.6 * s, 1.6 * s)

            painter.restore()

        # 5. Floating Holographic Energy Particles
        painter.setPen(Qt.PenStyle.NoPen)
        for p in self._particles:
            ang = p["angle"]
            tilt_rad = math.radians(p["tilt"])
            orbit_d = base_r * p["dist"] * (1.0 + 0.06 * math.sin(p["wobble_phase"]))

            # Project 3D tilted particle orbit onto 2D plane smoothly
            local_x = orbit_d * math.cos(ang)
            local_y = orbit_d * math.sin(ang) * 0.58
            px = cx + (local_x * math.cos(tilt_rad) - local_y * math.sin(tilt_rad))
            py = cy + (local_x * math.sin(tilt_rad) + local_y * math.cos(tilt_rad))
            pt = QPointF(px, py)

            twinkle = 0.65 + 0.35 * math.sin(p["wobble_phase"] + self.phase)
            p_alpha = int(min(255.0, 215.0 * p["alpha"] * twinkle + 35.0 * self._token_energy))
            p_size = p["size"] * s * (0.9 + 0.22 * self._token_energy)

            # Soft particle halo
            halo_col = QColor(
                self._cur_accent.red(),
                self._cur_accent.green(),
                self._cur_accent.blue(),
                int(p_alpha * 0.38),
            )
            painter.setBrush(halo_col)
            painter.drawEllipse(pt, p_size * 2.1, p_size * 2.1)

            # Bright particle spark
            spark_col = QColor(
                self._cur_core.red(),
                self._cur_core.green(),
                self._cur_core.blue(),
                p_alpha,
            )
            painter.setBrush(spark_col)
            painter.drawEllipse(pt, p_size, p_size)

        # 6. Central Holographic Energy Core (Multi-layered plasma nucleus)
        core_shell_r = base_r * (0.48 + 0.06 * self._token_energy)
        core_grad = QRadialGradient(
            QPointF(cx - core_shell_r * 0.18, cy - core_shell_r * 0.18),
            core_shell_r * 1.15,
        )
        core_grad.setColorAt(0.0, QColor(255, 255, 255, 255))
        core_grad.setColorAt(0.38, self._cur_core)
        core_grad.setColorAt(0.78, self._cur_mid)
        core_grad.setColorAt(
            1.0,
            QColor(
                self._cur_mid.red(),
                self._cur_mid.green(),
                self._cur_mid.blue(),
                30,
            ),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core_grad)
        painter.drawEllipse(center_f, core_shell_r, core_shell_r)

        # Crisp inner holographic containment filament around the core
        inner_filament_r = core_shell_r * 1.16
        filament_path = QPainterPath()
        filament_path.addEllipse(center_f, inner_filament_r, inner_filament_r)
        filament_col = QColor(self._cur_core)
        filament_col.setAlpha(int(120 + 70 * math.sin(self.phase * 1.5)))
        painter.setPen(QPen(filament_col, 1.1 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(filament_path)

    # ------------------------------------------------------------------
    # Mouse Click vs Drag Handling (X11 & Ubuntu Wayland Compatible)
    # ------------------------------------------------------------------
    @staticmethod
    def _event_global_point(event) -> QPoint:
        if hasattr(event, "globalPosition"):
            return event.globalPosition().toPoint()
        return event.globalPos()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global_pos = self._event_global_point(event)
            self._press_window_pos = self.pos()
            self._is_dragging = False
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._update_menu_labels()
            self.context_menu.popup(self._event_global_point(event))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            (event.buttons() & Qt.MouseButton.LeftButton)
            and self._press_global_pos is not None
            and self._press_window_pos is not None
        ):
            current_global = self._event_global_point(event)
            delta = current_global - self._press_global_pos

            if not self._is_dragging and delta.manhattanLength() >= self.DRAG_THRESHOLD:
                self._is_dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

                # On native Wayland sessions with real user input, hand off interactive
                # window dragging to the Wayland compositor (xdg_toplevel.move) once.
                platform_name = (QGuiApplication.platformName() or "").lower()
                if (
                    "wayland" in platform_name
                    and event.spontaneous()
                    and self.windowHandle() is not None
                ):
                    try:
                        if self.windowHandle().startSystemMove():
                            self._press_global_pos = None
                            self._press_window_pos = None
                            self._is_dragging = False
                            self.setCursor(Qt.CursorShape.PointingHandCursor)
                            event.accept()
                            return
                    except Exception:
                        pass

            if self._is_dragging:
                target_pos = self._press_window_pos + delta
                self.move(target_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._is_dragging
            self._press_global_pos = None
            self._press_window_pos = None
            self._is_dragging = False
            self.setCursor(Qt.CursorShape.PointingHandCursor)

            if not was_dragging:
                self.toggle_chat_window()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


def _run_standalone(argv: list[str] | None = None) -> int:
    """Delegate direct `python ui/orb.py` / `python -m ui.orb` execution to main.main()
    so the application always uses the same safe lifecycle and signal handlers."""
    from main import main

    return main(argv)


if __name__ == "__main__":
    sys.exit(_run_standalone())
