import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.memory.context_builder import MEMORY_FOOTER, MEMORY_HEADER, ContextBuilder
from services.memory.database import MemoryDatabase
from services.memory.repositories import (
    ConversationRepository,
    MemoryRepository,
    ProjectRepository,
    utc_now_iso,
)
from services.memory.retrieval import MemoryRetriever
from services.memory.text import compute_content_hash, normalize_tr


class TestStage8MemoryRetrieval(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "jarvis_retrieval.db"
        self.db = MemoryDatabase(self.db_path)
        self.db.connect()
        self.mem_repo = MemoryRepository(self.db)
        self.proj_repo = ProjectRepository(self.db)
        self.conv_repo = ConversationRepository(self.db)
        self.retriever = MemoryRetriever(self.db)

    def tearDown(self):
        self.db.close()
        self._tmpdir.cleanup()

    def test_turkish_character_folding_and_stem_matching(self):
        m_ist, _ = self.mem_repo.add_memory(
            "Kullanıcı İstanbul Kadıköy bölgesinde yaşıyor.",
            scope="global",
        )
        m_isik, _ = self.mem_repo.add_memory(
            "Gece modunda düşük ışık ve koyu tema tercih ediliyor.",
            scope="global",
        )
        m_proje, _ = self.mem_repo.add_memory(
            "Proje mimarisinde PySide6 ve SQLite FTS5 kullanılıyor.",
            scope="global",
        )

        # Query with lowercase ASCII 'istanbul' must match 'İstanbul'
        res_ist = self.retriever.retrieve("istanbul kadikoy", include_pinned=False)
        self.assertIn(m_ist.id, [r.record.id for r in res_ist])

        # Query with 'ışık' or 'isik' must match
        res_isik = self.retriever.retrieve("isik", include_pinned=False)
        self.assertIn(m_isik.id, [r.record.id for r in res_isik])

        # Inflected Turkish word 'projesinin' must stem-match 'proje'
        res_proj = self.retriever.retrieve("projesinin mimarisi", include_pinned=False)
        self.assertIn(m_proje.id, [r.record.id for r in res_proj])

    def test_strict_scope_isolation_between_projects_and_conversations(self):
        proj_a = self.proj_repo.create_or_get("Project Alpha")
        proj_b = self.proj_repo.create_or_get("Project Beta")
        conv_1 = self.conv_repo.create_conversation(project_id=proj_a.id)
        conv_2 = self.conv_repo.create_conversation(project_id=proj_b.id)

        m_global, _ = self.mem_repo.add_memory(
            "Veritabanı bağlantı havuzu ortak ayarları.",
            scope="global",
        )
        m_proj_a, _ = self.mem_repo.add_memory(
            "Veritabanı bağlantı şeması Alpha mikroservisi için PostgreSQL.",
            scope="project",
            project_id=proj_a.id,
        )
        m_proj_b, _ = self.mem_repo.add_memory(
            "Veritabanı bağlantı şeması Beta uygulaması için Redis.",
            scope="project",
            project_id=proj_b.id,
        )
        m_conv_1, _ = self.mem_repo.add_memory(
            "Veritabanı bağlantı hatası bu oturumda geçici olarak çözüldü.",
            scope="conversation",
            conversation_id=conv_1.id,
        )

        # 1. Query inside (proj_a, conv_1): sees global + proj_a + conv_1, NEVER proj_b
        res_a1 = self.retriever.retrieve(
            "veritabanı bağlantı",
            project_id=proj_a.id,
            conversation_id=conv_1.id,
        )
        ids_a1 = {r.record.id for r in res_a1}
        self.assertIn(m_global.id, ids_a1)
        self.assertIn(m_proj_a.id, ids_a1)
        self.assertIn(m_conv_1.id, ids_a1)
        self.assertNotIn(m_proj_b.id, ids_a1)

        # 2. Query inside (proj_b, conv_2): sees global + proj_b, NEVER proj_a or conv_1
        res_b2 = self.retriever.retrieve(
            "veritabanı bağlantı",
            project_id=proj_b.id,
            conversation_id=conv_2.id,
        )
        ids_b2 = {r.record.id for r in res_b2}
        self.assertIn(m_global.id, ids_b2)
        self.assertIn(m_proj_b.id, ids_b2)
        self.assertNotIn(m_proj_a.id, ids_b2)
        self.assertNotIn(m_conv_1.id, ids_b2)

        # 3. Query with no active project (project_id=None, conv_2): sees only global
        res_none = self.retriever.retrieve(
            "veritabanı bağlantı",
            project_id=None,
            conversation_id=conv_2.id,
        )
        ids_none = {r.record.id for r in res_none}
        self.assertEqual(ids_none, {m_global.id})

    def test_pending_and_deleted_memories_are_never_retrieved(self):
        m_pending, _ = self.mem_repo.add_memory(
            "Kullanıcı Rust dilini öğrenmek istiyor.",
            scope="global",
            status="pending",
            origin="extracted",
        )
        m_active, _ = self.mem_repo.add_memory(
            "Kullanıcı Python dilini aktif kullanıyor.",
            scope="global",
            status="active",
        )
        m_deleted, _ = self.mem_repo.add_memory(
            "Kullanıcı eski bir Java projesini kapattı.",
            scope="global",
            status="active",
        )
        self.mem_repo.delete_memory(m_deleted.id, hard=False)

        # Search for 'dilini' or bootstrap must NEVER return pending or deleted
        res = self.retriever.retrieve("dilini proje", include_pinned=True)
        res_ids = {r.record.id for r in res}
        self.assertIn(m_active.id, res_ids)
        self.assertNotIn(m_pending.id, res_ids)
        self.assertNotIn(m_deleted.id, res_ids)

        boot = self.retriever.retrieve_for_bootstrap(query="dilini")
        boot_ids = {r.record.id for r in boot}
        self.assertIn(m_active.id, boot_ids)
        self.assertNotIn(m_pending.id, boot_ids)
        self.assertNotIn(m_deleted.id, boot_ids)

        # Once user explicitly confirms m_pending, it becomes retrievable
        self.mem_repo.confirm_pending(m_pending.id)
        res_after = self.retriever.retrieve("Rust dilini", include_pinned=False)
        self.assertIn(m_pending.id, {r.record.id for r in res_after})

    def test_composite_ranking_pinned_boost_and_budget_enforcement(self):
        m_low, _ = self.mem_repo.add_memory(
            "Docker konteyner yapılandırması standart.",
            scope="global",
            importance=1,
            pinned=False,
        )
        m_pinned, _ = self.mem_repo.add_memory(
            "Docker konteyner güvenliği için rootless mod zorunludur.",
            scope="global",
            importance=5,
            pinned=True,
        )

        ranked = self.retriever.retrieve("Docker konteyner", include_pinned=True)
        self.assertGreaterEqual(len(ranked), 2)
        self.assertEqual(ranked[0].record.id, m_pinned.id)
        self.assertGreater(ranked[0].score, ranked[1].score)

        # Budget cap check: max_items=1
        capped = self.retriever.retrieve("Docker konteyner", max_items=1)
        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0].record.id, m_pinned.id)

        # ContextBuilder formatting check
        builder = ContextBuilder()
        prompt = builder.build_prompt(
            user_message="Docker ayarlarını nasıl yapmalıyım?",
            memories=capped,
            is_bootstrap=True,
        )
        self.assertIn(MEMORY_HEADER, prompt)
        self.assertIn(MEMORY_FOOTER, prompt)
        self.assertIn("rootless mod zorunludur", prompt)
        self.assertTrue(prompt.endswith("Docker ayarlarını nasıl yapmalıyım?"))

    def test_fts5_10k_rows_retrieval_latency_under_50ms(self):
        now = utc_now_iso()
        batch = []
        for i in range(10000):
            text = f"Sentetik telemetri kaydı numara {i} sistem bileşeni modül {i % 97}"
            if i == 7777:
                text = "Özel kuantum şifreleme protokolü RTX4060 hızlandırma anahtarı"
            s_text = normalize_tr(text)
            c_hash = compute_content_hash(text)
            batch.append(
                (
                    "global",
                    None,
                    None,
                    "fact",
                    text,
                    s_text,
                    c_hash,
                    (i % 5) + 1,
                    "active",
                    "explicit",
                    0,
                    now,
                    now,
                )
            )

        with self.db.connection:
            self.db.connection.executemany(
                "INSERT INTO memories("
                "  scope, project_id, conversation_id, kind, content, search_text, content_hash, "
                "  importance, status, origin, pinned, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                batch,
            )

        t0 = time.perf_counter()
        results = self.retriever.retrieve("kuantum şifreleme protokolü RTX4060", include_pinned=False)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertGreaterEqual(len(results), 1)
        self.assertIn("kuantum şifreleme protokolü", results[0].record.content)
        self.assertLess(
            elapsed_ms,
            50.0,
            f"10k FTS5 retrieval took {elapsed_ms:.2f} ms (expected < 50 ms)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
