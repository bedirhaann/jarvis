import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui.chat import ChatWindow
from ui.orb import JarvisOrb


def _make_mouse_event(
    event_type: QEvent.Type,
    local_pos: QPoint,
    global_pos: QPoint,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
) -> QMouseEvent:
    return QMouseEvent(
        event_type,
        QPointF(local_pos),
        QPointF(global_pos),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


class TestStage4Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.chat = ChatWindow(start_worker=False)
        self.orb = JarvisOrb(start_worker=False, chat_window=self.chat)
        self.orb.show()
        self.app.processEvents()

    def tearDown(self):
        self.orb.timer.stop()
        self.chat.close()
        self.orb.close()
        self.app.processEvents()

    def test_state_synchronization_and_animations_for_all_five_states(self):
        # Initial state is connecting
        self.assertEqual(self.orb.state(), "connecting")

        # Simulate ChatWindow state changes for all 5 states
        for state in ("idle", "thinking", "responding", "error", "connecting"):
            self.chat.state_changed.emit(state)
            self.app.processEvents()
            self.assertEqual(self.orb.state(), state)

            # Run several animation ticks and repaint to verify painting & color lerp
            prev_phase = self.orb.phase
            for _ in range(10):
                self.orb.animate()
            self.orb.repaint()
            self.app.processEvents()
            self.assertGreater(self.orb.phase, prev_phase)

    def test_token_streamed_synchronization_boosts_energy_and_sets_responding(self):
        self.orb.set_state("thinking")
        self.assertEqual(self.orb.token_energy(), 0.0)

        # ChatWindow streams a token -> Orb switches to responding and boosts token_energy
        self.chat.on_ready()
        self.chat.input.setText("Merhaba")
        self.chat.send_message()
        self.assertEqual(self.orb.state(), "thinking")

        self.chat.on_token("Selam ")
        self.app.processEvents()
        self.assertEqual(self.orb.state(), "responding")
        self.assertGreater(self.orb.token_energy(), 0.3)

        # Energy decays smoothly over animation ticks
        initial_energy = self.orb.token_energy()
        for _ in range(5):
            self.orb.animate()
        self.assertLess(self.orb.token_energy(), initial_energy)

        self.chat.on_finished()
        self.app.processEvents()
        self.assertEqual(self.orb.state(), "idle")

    def test_left_click_toggles_chat_window_open_and_close(self):
        self.assertFalse(self.chat.isVisible())

        local_pt = QPoint(60, 60)
        global_pt = self.orb.mapToGlobal(local_pt)

        # 1st left click -> opens ChatWindow
        press1 = _make_mouse_event(
            QEvent.Type.MouseButtonPress,
            local_pt,
            global_pt,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        release1 = _make_mouse_event(
            QEvent.Type.MouseButtonRelease,
            local_pt,
            global_pt,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        self.orb.mousePressEvent(press1)
        self.orb.mouseReleaseEvent(release1)
        self.app.processEvents()
        self.assertTrue(self.chat.isVisible())

        # 2nd left click -> hides ChatWindow
        press2 = _make_mouse_event(
            QEvent.Type.MouseButtonPress,
            local_pt,
            global_pt,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        release2 = _make_mouse_event(
            QEvent.Type.MouseButtonRelease,
            local_pt,
            global_pt,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
        self.orb.mousePressEvent(press2)
        self.orb.mouseReleaseEvent(release2)
        self.app.processEvents()
        self.assertFalse(self.chat.isVisible())

    def test_mouse_drag_moves_orb_without_toggling_chat_window(self):
        self.orb.move(100, 100)
        self.app.processEvents()
        start_pos = self.orb.pos()
        self.assertFalse(self.chat.isVisible())

        local_pt = QPoint(60, 60)
        global_start = QPoint(160, 160)
        global_dragged = QPoint(240, 210)  # Delta (+80, +50) > DRAG_THRESHOLD

        press = _make_mouse_event(
            QEvent.Type.MouseButtonPress,
            local_pt,
            global_start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        move = _make_mouse_event(
            QEvent.Type.MouseMove,
            local_pt,
            global_dragged,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        release = _make_mouse_event(
            QEvent.Type.MouseButtonRelease,
            local_pt,
            global_dragged,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )

        self.orb.mousePressEvent(press)
        self.orb.mouseMoveEvent(move)
        self.orb.mouseReleaseEvent(release)
        self.app.processEvents()

        # Orb moved by (+80, +50) and did NOT open chat window on drag release
        self.assertEqual(self.orb.pos(), start_pos + QPoint(80, 50))
        self.assertFalse(self.chat.isVisible())

    def test_context_menu_actions_toggle_clear_reset_and_quit(self):
        menu_actions = [a.text() for a in self.orb.context_menu.actions() if not a.isSeparator()]
        self.assertEqual(len(menu_actions), 4)

        # 1. Sohbeti Aç/Gizle via context menu action
        self.assertFalse(self.chat.isVisible())
        self.orb.action_toggle_chat.trigger()
        self.app.processEvents()
        self.assertTrue(self.chat.isVisible())

        # 2. Sohbeti Temizle via context menu action
        self.chat.add_user_message("Test mesajı")
        self.chat.add_assistant_message("Test yanıtı")
        self.assertGreaterEqual(len(self.chat.bubbles()), 2)
        self.orb.action_clear_chat.trigger()
        self.app.processEvents()
        self.assertEqual(len(self.chat.bubbles()), 0)

        # 3. Köşeye Sıfırla via context menu action
        self.orb.move(15, 25)
        self.assertEqual(self.orb.pos(), QPoint(15, 25))
        self.orb.action_reset_corner.trigger()
        self.app.processEvents()
        self.assertNotEqual(self.orb.pos(), QPoint(15, 25))

        # 4. Çıkış via context menu action initiates non-blocking shutdown
        self.orb.action_quit.trigger()
        self.app.processEvents()
        self.assertTrue(self.chat.is_shutdown_complete())
        self.assertFalse(self.orb.isVisible())

    def test_stage6_holographic_orbitals_and_particles_render_cleanly(self):
        # Verify Stage 6.2 double scale: 432x432 window, ORB_SCALE=3.00, 300-315px sphere diameter
        self.assertEqual(self.orb.width(), 432)
        self.assertEqual(self.orb.height(), 432)
        self.assertAlmostEqual(self.orb.ORB_SCALE, 3.00)
        diameter = self.orb.visible_sphere_diameter()
        self.assertGreaterEqual(diameter, 300.0)
        self.assertLessEqual(diameter, 315.0)

        # Verify 3 elliptical orbital rings and pre-allocated floating particles
        initial_orbits = self.orb.orbital_angles()
        self.assertEqual(len(initial_orbits), 3)
        particles = self.orb.particles()
        self.assertGreaterEqual(len(particles), 10)
        initial_particle_ids = [id(p) for p in particles]
        initial_particle_angles = [p["angle"] for p in particles]

        cx = self.orb.width() // 2
        cy = self.orb.height() // 2

        for state in ("idle", "thinking", "responding", "connecting", "error"):
            self.orb.set_state(state)
            for _ in range(6):
                self.orb.animate()
            pixmap = self.orb.grab()
            self.assertFalse(pixmap.isNull())
            img = pixmap.toImage()
            # Center pixel of the holographic energy core must be bright and non-transparent
            center_color = img.pixelColor(cx, cy)
            self.assertGreater(center_color.alpha(), 200)
            # Scaled outer aura / containment region at +100px from center must be rendered
            scaled_aura_color = img.pixelColor(cx + 100, cy)
            self.assertGreater(scaled_aura_color.alpha(), 25)
            # Outer boundary pixels must remain transparent (no clipping at window edges)
            self.assertEqual(img.pixelColor(0, cy).alpha(), 0)
            self.assertEqual(img.pixelColor(self.orb.width() - 1, cy).alpha(), 0)

        # Verify all 3 orbital rings rotated independently
        updated_orbits = self.orb.orbital_angles()
        for i in range(3):
            self.assertNotAlmostEqual(initial_orbits[i], updated_orbits[i])

        # Verify particles moved without re-allocating particle dicts
        self.assertEqual([id(p) for p in self.orb.particles()], initial_particle_ids)
        self.assertNotEqual([p["angle"] for p in self.orb.particles()], initial_particle_angles)


if __name__ == "__main__":
    unittest.main(verbosity=2)
