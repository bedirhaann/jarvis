import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtWidgets import QApplication
from ui.markdown_renderer import is_safe_http_url, render_markdown
from ui.widgets import MessageBubble


class TestStage2Markdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_streaming_unclosed_code_block_preserved(self):
        partial_md = "İşte örnek kod:\n```python\ndef hesapla(x):\n    return x * 2"
        rendered_streaming = render_markdown(partial_md, is_streaming=True)

        self.assertIn("PYTHON", rendered_streaming)
        self.assertIn("yazılıyor...", rendered_streaming)
        self.assertIn("def hesapla(x):", rendered_streaming)
        self.assertIn("return x * 2", rendered_streaming)
        self.assertIn("<pre", rendered_streaming)

        # Complete the fence
        complete_md = partial_md + "\n```\nİşlem tamamlandı."
        rendered_final = render_markdown(complete_md, is_streaming=False)

        self.assertIn("PYTHON", rendered_final)
        self.assertNotIn("yazılıyor...", rendered_final)
        self.assertIn("def hesapla(x):", rendered_final)
        self.assertIn("İşlem tamamlandı.", rendered_final)

    def test_streaming_unclosed_inline_formatting_preserved(self):
        # Partial bold
        html_bold = render_markdown("Bu çok **önemli bir", is_streaming=True)
        self.assertIn("<b", html_bold)
        self.assertIn("önemli bir</b>", html_bold)

        # Partial inline code
        html_code = render_markdown("Komut: `systemctl status", is_streaming=True)
        self.assertIn("<code", html_code)
        self.assertIn("systemctl status</code>", html_code)

        # Partial italic
        html_italic = render_markdown("Not: *dikkatli olun", is_streaming=True)
        self.assertIn("<i", html_italic)
        self.assertIn("dikkatli olun</i>", html_italic)

    def test_raw_html_from_model_is_strictly_escaped(self):
        malicious = (
            "Merhaba <script>alert('xss')</script> ve "
            "<img src=x onerror=alert(1)> ayrıca "
            "```html\n<script>evil()</script>\n```"
        )
        rendered = render_markdown(malicious, is_streaming=False)

        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", rendered)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertIn("&lt;script&gt;evil()&lt;/script&gt;", rendered)

    def test_url_protocol_validation_blocks_javascript_data_and_file(self):
        self.assertTrue(is_safe_http_url("https://docs.python.org/3/"))
        self.assertTrue(is_safe_http_url("http://example.com/test"))

        self.assertFalse(is_safe_http_url("javascript:alert(1)"))
        self.assertFalse(is_safe_http_url("JaVaScRiPt:alert(1)"))
        self.assertFalse(is_safe_http_url("data:text/html,<script>alert(1)</script>"))
        self.assertFalse(is_safe_http_url("file:///etc/passwd"))
        self.assertFalse(is_safe_http_url("vbscript:msgbox(1)"))

        md_links = (
            "[Güvenli](https://python.org) | "
            "[JS](javascript:alert(1)) | "
            "[Data](data:text/html,<h1>x</h1>) | "
            "[File](file:///home/bedirhan/.bashrc)"
        )
        rendered = render_markdown(md_links, is_streaming=False)

        # Safe https link is converted to <a href="...">
        self.assertIn('<a href="https://python.org"', rendered)

        # Dangerous links must NEVER become <a href="...">
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn('href="data:', rendered)
        self.assertNotIn('href="file:', rendered)

    def test_message_bubble_token_by_token_streaming(self):
        bubble = MessageBubble(role="assistant")
        tokens = [
            "### Başlık\n",
            "Bu **kalın",
            "** ve `satır_içi` ",
            "kod örneği:\n",
            "```bash\n",
            "echo 'JARVIS'",
            "\n```",
        ]
        for tok in tokens:
            bubble.append_token(tok)
            self.assertTrue(bubble.is_streaming())

        bubble.finalize()
        self.assertFalse(bubble.is_streaming())
        self.assertEqual(bubble.raw_text(), "".join(tokens))
        html_out = bubble.body.toHtml()
        self.assertIn("Başlık", html_out)
        self.assertIn("echo", html_out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
