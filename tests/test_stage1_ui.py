import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtWidgets import QApplication
from ui.chat import ChatWindow
from ui.widgets import MessageBubble, ThinkingIndicator


class TestStage1UI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.chat = ChatWindow(start_worker=False)
        self.chat.show()
        self.app.processEvents()

    def tearDown(self):
        self.chat.close()
        self.app.processEvents()

    def test_distinct_user_and_assistant_bubbles(self):
        initial_count = len(self.chat.bubbles())
        self.assertEqual(initial_count, 1)  # System startup bubble
        self.assertEqual(self.chat.bubbles()[0].role, "system")

        user_bubble = self.chat.add_user_message("Merhaba JARVIS")
        asst_bubble = self.chat.add_assistant_message("Merhaba Bedirhan, nasıl yardımcı olabilirim?")

        self.assertIsInstance(user_bubble, MessageBubble)
        self.assertIsInstance(asst_bubble, MessageBubble)
        self.assertEqual(user_bubble.role, "user")
        self.assertEqual(asst_bubble.role, "assistant")
        self.assertEqual(user_bubble.sender_label.text(), "BEDİRHAN")
        self.assertEqual(asst_bubble.sender_label.text(), "J.A.R.V.I.S.")
        self.assertIsNone(user_bubble.copy_button)
        self.assertIsNotNone(asst_bubble.copy_button)

    def test_thinking_indicator_lifecycle(self):
        self.chat.on_ready()
        self.app.processEvents()

        self.assertFalse(self.chat.thinking_indicator.is_active())

        self.chat.input.setText("Bana Python hakkında bilgi ver")
        self.chat.send_message()
        self.app.processEvents()

        # Thinking indicator must be active right after sending message
        self.assertTrue(self.chat.thinking_indicator.is_active())
        self.assertTrue(self.chat.thinking_indicator.isVisible())

        # First token arrives -> thinking indicator stops and assistant bubble starts
        self.chat.on_token("Python ")
        self.app.processEvents()

        self.assertFalse(self.chat.thinking_indicator.is_active())
        self.assertEqual(self.chat.bubbles()[-1].role, "assistant")
        self.assertEqual(self.chat.bubbles()[-1].raw_text(), "Python ")

        self.chat.on_token("güçlü bir dildir.")
        self.chat.on_finished()
        self.app.processEvents()

        self.assertEqual(
            self.chat.bubbles()[-1].raw_text(),
            "Python güçlü bir dildir."
        )
        self.assertFalse(self.chat.bubbles()[-1].is_streaming())

    def test_smart_auto_scroll_disables_when_user_scrolls_up(self):
        self.chat.on_ready()
        # Populate enough messages to create a scrollable range
        for i in range(18):
            self.chat.add_user_message(f"Kullanıcı mesajı satır {i} - " + ("veri " * 15))
            self.chat.add_assistant_message(f"Asistan yanıtı satır {i} - " + ("detay " * 20))
        self.app.processEvents()

        vbar = self.chat.scroll_area.verticalScrollBar()
        self.assertGreater(vbar.maximum(), 100)
        self.assertTrue(self.chat.is_auto_scroll_enabled())
        self.assertEqual(vbar.value(), vbar.maximum())

        # Simulate user manually scrolling up to the top
        vbar.setValue(10)
        self.app.processEvents()

        self.assertFalse(self.chat.is_auto_scroll_enabled())
        saved_scroll_pos = vbar.value()

        # Stream new tokens while user is scrolled up -> scrollbar MUST NOT jump to bottom
        self.chat.on_token("Yeni gelen uzun akış parçası 1\n" * 6)
        self.app.processEvents()
        self.chat.on_token("Yeni gelen uzun akış parçası 2\n" * 6)
        self.app.processEvents()

        self.assertFalse(self.chat.is_auto_scroll_enabled())
        self.assertEqual(vbar.value(), saved_scroll_pos)

        # Simulate user scrolling back to bottom -> auto-scroll re-engages
        vbar.setValue(vbar.maximum())
        self.app.processEvents()
        self.assertTrue(self.chat.is_auto_scroll_enabled())

    def test_weather_badge_signal_and_button(self):
        self.assertTrue(hasattr(self.chat, "weather_button"))
        self.assertEqual(self.chat.weather_button.text(), "⛅ Hava")

        # Worker emits weather_badge_updated -> ChatWindow.update_weather_badge updates button
        self.chat.worker.weather_badge_updated.emit("☀️ 21°C")
        self.app.processEvents()
        self.assertEqual(self.chat.weather_button.text(), "☀️ 21°C")

        # Empty string safely resets to default label
        self.chat.update_weather_badge("   ")
        self.assertEqual(self.chat.weather_button.text(), "⛅ Hava")

        # Clicking weather_button when ready submits '/hava'
        self.chat.on_ready()
        requested_messages: list[str] = []
        self.chat.message_requested.connect(requested_messages.append)
        self.chat.weather_button.click()
        self.app.processEvents()
        self.assertEqual(requested_messages, ["/hava"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
