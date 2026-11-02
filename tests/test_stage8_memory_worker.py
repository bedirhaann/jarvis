import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtWidgets import QApplication

from core.app_context import AppContext
from services.antigravity_worker import AntigravityWorker
from services.memory.service import MemoryService
from ui.chat import ChatWindow


class ScriptedAntigravityClient:
    """Deterministic fake AntigravityClient recording all prompts sent to ask()."""

    _instance_counter = 0

    def __init__(self, crash_on_call: int | None = None, response_text: str = "Tamam, anlaşıldı."):
        ScriptedAntigravityClient._instance_counter += 1
        self.instance_id = ScriptedAntigravityClient._instance_counter
        self.conversation_id: str | None = None
        self.model = "gemini-4-argon-medium"
        self.mode = "plan"
        self.started = False
        self.closed = False
        self.healthy = True
        self.ask_prompts: list[str] = []
        self.crash_on_call = crash_on_call
        self.response_text = response_text
        self._call_count = 0

    def start(self, timeout: float = 60.0) -> str:
        self.started = True
        self.closed = False
        self.healthy = True
        self.conversation_id = f"agy-session-{self.instance_id}"
        return self.conversation_id

    def is_healthy(self) -> bool:
        return self.started and not self.closed and self.healthy

    def request_close(self) -> None:
        self.closed = True
        self.healthy = False

    def close(self) -> None:
        self.closed = True
        self.healthy = False

    def ask(self, prompt: str, on_event=None, timeout: float = 180.0, on_wait=None) -> None:
        self._call_count += 1
        self.ask_prompts.append(prompt)
        if self.crash_on_call is not None and self._call_count == self.crash_on_call:
            if on_event is not None:
                on_event({"event": "step_update", "step_update": {"text_delta": "Kısmi yanıt verisi"}})
            self.healthy = False
            raise RuntimeError("Subprocess EOF: agy beklenmedik biçimde kapandı.")

        if on_event is not None:
            on_event({"event": "step_update", "step_update": {"text_delta": self.response_text}})


class TestStage8MemoryWorkerAndUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.db_path = self.tmp_path / "jarvis.db"
        self.clients: list[ScriptedAntigravityClient] = []

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_client_factory(self, crash_first_client_on_call: int | None = None):
        def _factory():
            crash_call = crash_first_client_on_call if len(self.clients) == 0 else None
            c = ScriptedAntigravityClient(crash_on_call=crash_call)
            self.clients.append(c)
            return c

        return _factory

    def test_local_commands_never_touch_antigravity_client(self):
        worker = AntigravityWorker(
            client_factory=self._make_client_factory(),
            memory_service_factory=lambda: MemoryService(db_path=self.db_path),
        )
        tokens: list[str] = []
        worker.token_received.connect(tokens.append)
        worker.initialize()

        try:
            self.assertEqual(len(self.clients), 1)
            client = self.clients[0]

            # 1. /hatırla
            tokens.clear()
            worker.send_message("/hatırla --sabit Kullanıcının ana işletim sistemi Ubuntu Linux.")
            out_hatirla = "".join(tokens)
            self.assertIn("Hafızaya kaydedildi", out_hatirla)
            self.assertEqual(len(client.ask_prompts), 0)

            # 2. /proje
            tokens.clear()
            worker.send_message("/proje jarvis-asistan")
            out_proje = "".join(tokens)
            self.assertIn("Aktif proje değiştirildi", out_proje)
            self.assertEqual(len(client.ask_prompts), 0)

            # 3. Project-scoped /hatırla
            tokens.clear()
            worker.send_message("/hatırla proje: Arayüz katmanında PySide6 6.12 kullanılıyor.")
            out_proj_mem = "".join(tokens)
            self.assertIn("Proje", out_proj_mem)
            self.assertEqual(len(client.ask_prompts), 0)

            # 4. /hafıza & /hafıza ara
            tokens.clear()
            worker.send_message("/hafıza")
            out_list = "".join(tokens)
            self.assertIn("Ubuntu Linux", out_list)
            self.assertIn("PySide6 6.12", out_list)

            tokens.clear()
            worker.send_message("/hafıza ara ubuntu")
            out_search = "".join(tokens)
            self.assertIn("Ubuntu Linux", out_search)

            # 5. /geçmiş
            tokens.clear()
            worker.send_message("/geçmiş")
            out_hist = "".join(tokens)
            self.assertIn("Son Konuşmalar", out_hist)

            # 6. /unut (soft-delete then hard-delete)
            tokens.clear()
            worker.send_message("/unut 1")
            self.assertIn("soft-delete", "".join(tokens))

            tokens.clear()
            worker.send_message("/unut 2 --kalıcı")
            self.assertIn("hard-delete", "".join(tokens))

            # Verify zero LLM calls occurred across all local commands
            self.assertEqual(len(client.ask_prompts), 0)
        finally:
            worker.shutdown()

    def test_user_confirmed_pending_suggestion_workflow(self):
        worker = AntigravityWorker(
            client_factory=self._make_client_factory(),
            memory_service_factory=lambda: MemoryService(db_path=self.db_path),
        )
        tokens: list[str] = []
        worker.token_received.connect(tokens.append)
        worker.initialize()

        try:
            client = self.clients[0]

            # 1. User sends a message with an implicit preference ("bundan sonra ...")
            tokens.clear()
            worker.send_message("Merhaba, bundan sonra kod örneklerinde tip anotasyonu kullan.")
            first_reply = "".join(tokens)

            # Suggestion notice must be presented to the user
            self.assertIn("Hafıza Önerisi", first_reply)
            self.assertIn("/hafıza onayla", first_reply)

            # Pending suggestion must exist in DB with status='pending'
            assert worker.memory_service is not None
            pending = worker.memory_service.list_pending_memories()
            self.assertEqual(len(pending), 1)
            sug_id = pending[0].id
            self.assertEqual(pending[0].status, "pending")

            # 2. Send another message BEFORE confirming -> pending memory MUST NOT be injected!
            worker.send_message("Bana tip anotasyonu hakkında kısa bilgi ver.")
            second_prompt = client.ask_prompts[-1]
            self.assertNotIn("[JARVIS_MEMORY", second_prompt)

            # 3. User edits and confirms the suggestion via '/hafıza düzenle <id> <metin>'
            tokens.clear()
            worker.send_message(
                f"/hafıza düzenle {sug_id} Kod örneklerinde her zaman Python 3.14 tip anotasyonu kullan."
            )
            self.assertIn("güncellendi ve aktifleştirildi", "".join(tokens))
            self.assertEqual(len(worker.memory_service.list_pending_memories()), 0)

            # 4. Now send a relevant message -> confirmed memory MUST be injected into LLM prompt!
            worker.send_message("Python tip anotasyonu örneği yaz.")
            third_prompt = client.ask_prompts[-1]
            self.assertIn("[JARVIS_MEMORY v1", third_prompt)
            self.assertIn("Python 3.14 tip anotasyonu kullan", third_prompt)

            # 5. Test rejection workflow (/hafıza reddet <id>)
            worker.send_message("Ayrıca benim adım Bedirhan.")
            pending_2 = worker.memory_service.list_pending_memories()
            self.assertEqual(len(pending_2), 1)
            reject_id = pending_2[0].id

            tokens.clear()
            worker.send_message(f"/hafıza reddet {reject_id}")
            self.assertIn("reddedildi", "".join(tokens))
            self.assertEqual(len(worker.memory_service.list_pending_memories()), 0)
        finally:
            worker.shutdown()

    def _wait_until(self, predicate, timeout_s: float = 3.0):
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return predicate()

    def test_temizle_button_starts_new_conversation_and_fresh_agy_session_preserving_memories(self):
        app_ctx = AppContext(
            db_path=self.db_path,
            memory_enabled=True,
        )
        worker = AntigravityWorker(
            client_factory=self._make_client_factory(),
            memory_service_factory=lambda: MemoryService(db_path=self.db_path),
        )
        chat = ChatWindow(start_worker=True, app_context=app_ctx, worker=worker)
        chat.show()

        try:
            self.assertTrue(
                self._wait_until(lambda: chat.input.isEnabled() and len(self.clients) == 1)
            )
            assert worker.memory_service is not None
            conv_1_id = worker.memory_service.current_conversation_id
            self.assertIsNotNone(conv_1_id)
            client_1 = self.clients[0]

            # Save a global memory and a project memory via UI
            for cmd in (
                "/hatırla --sabit Kullanıcının favori editörü Neovim.",
                "/proje kuantum-motoru",
                "/hatırla proje: Kuantum motoru Rust ve PyO3 ile yazılıyor.",
            ):
                chat.input.setText(cmd)
                chat.send_message()
                self.assertTrue(self._wait_until(lambda: chat.input.isEnabled()))

            # Send a conversation message in Conversation #1
            chat.input.setText("Bu oturumun gizli kodu Zümrüt Anka 42.")
            chat.send_message()
            self.assertTrue(
                self._wait_until(lambda: chat.input.isEnabled() and len(client_1.ask_prompts) == 1)
            )

            # User clicks "Temizle" button in ChatWindow
            chat.clear_button.click()
            self.assertTrue(
                self._wait_until(
                    lambda: client_1.closed
                    and worker.memory_service.current_conversation_id != conv_1_id
                    and len(chat.bubbles()) >= 1
                )
            )

            # Verify:
            # 1. Old AntigravityClient session was closed
            self.assertTrue(client_1.closed)
            # 2. A new conversation record was created in SQLite
            conv_2_id = worker.memory_service.current_conversation_id
            self.assertIsNotNone(conv_2_id)
            self.assertNotEqual(conv_1_id, conv_2_id)
            # 3. UI clearly shows a new conversation started
            self.assertEqual(chat.status.text(), "● NEW SESSION")
            self.assertGreaterEqual(len(chat.bubbles()), 1)
            self.assertEqual(chat.bubbles()[-1].role, "system")
            self.assertIn("Yeni konuşma başlatıldı", chat.bubbles()[-1].raw_text())

            # 4. Send a message in the new conversation
            chat.input.setText("Kuantum motoru geliştirme ortamım nedir?")
            chat.send_message()
            self.assertTrue(
                self._wait_until(lambda: chat.input.isEnabled() and len(self.clients) == 2)
            )

            # A fresh AntigravityClient (client_2) must have been spawned
            client_2 = self.clients[1]
            self.assertNotEqual(client_1.conversation_id, client_2.conversation_id)
            self.assertEqual(len(client_2.ask_prompts), 1)

            new_session_prompt = client_2.ask_prompts[0]
            # Global and project memories MUST be preserved and bootstrapped into client_2
            self.assertIn("Neovim", new_session_prompt)
            self.assertIn("Rust ve PyO3", new_session_prompt)
            # Previous conversation's messages MUST NOT be carried into the new conversation!
            self.assertNotIn("Zümrüt Anka 42", new_session_prompt)
        finally:
            chat.request_shutdown()
            self._wait_until(lambda: chat.verify_threads_stopped(wait_ms=50))
            chat.close()

    def test_crash_and_restart_continuity_with_partial_message_recovery(self):
        # First worker instance: client_1 crashes on its 2nd LLM call
        worker1 = AntigravityWorker(
            client_factory=self._make_client_factory(crash_first_client_on_call=2),
            memory_service_factory=lambda: MemoryService(db_path=self.db_path),
        )
        errors: list[str] = []
        worker1.error_occurred.connect(errors.append)
        worker1.initialize()

        try:
            worker1.send_message("/hatırla --sabit Tercih edilen veritabanı SQLite WAL modudur.")
            worker1.send_message("Merhaba, ilk mesajım sorunsuz tamamlanmalı.")
            self.assertEqual(len(errors), 0)

            # 2nd LLM call crashes mid-stream after emitting "Kısmi yanıt verisi"
            worker1.send_message("Bu mesaj sırasında alt süreç çökecek.")
            self.assertEqual(len(errors), 1)
            self.assertTrue(self.clients[0].closed)

            # Verify partial assistant message was persisted with status='partial'
            assert worker1.memory_service is not None
            conv_id = worker1.memory_service.current_conversation_id
            assert conv_id is not None
            msgs = worker1.memory_service.list_conversation_messages(conv_id)
            statuses = [m.status for m in msgs if m.role == "assistant"]
            self.assertIn("partial", statuses)

            # Next message in the SAME conversation spawns client_2 and bootstraps both
            # global memories AND recent turns of the current conversation!
            worker1.send_message("Çökme sonrası kaldığımız yerden devam edelim.")
            self.assertEqual(len(self.clients), 2)
            recovery_prompt = self.clients[1].ask_prompts[0]
            self.assertIn("SQLite WAL modudur", recovery_prompt)
            self.assertIn("Current Conversation Recent Turns:", recovery_prompt)
            self.assertIn("ilk mesajım sorunsuz tamamlanmalı", recovery_prompt)
        finally:
            worker1.shutdown()

        # Simulate full application restart: new worker opening the same jarvis.db on disk
        worker2 = AntigravityWorker(
            client_factory=self._make_client_factory(),
            memory_service_factory=lambda: MemoryService(db_path=self.db_path),
        )
        worker2.initialize()
        try:
            worker2.send_message("Yeni uygulama açılışında veritabanı tercihim nedir?")
            restart_prompt = self.clients[-1].ask_prompts[0]
            self.assertIn("SQLite WAL modudur", restart_prompt)
        finally:
            worker2.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
