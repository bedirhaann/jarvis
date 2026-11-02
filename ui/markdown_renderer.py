import html
import re
from urllib.parse import urlsplit


ALLOWED_LINK_SCHEMES = {"http", "https"}

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*(?:---|\*\*\*|___)\s*$")


def is_safe_http_url(raw_url: str) -> bool:
    """Validate that a URL uses strictly http:// or https:// with a non-empty host.
    Rejects javascript:, data:, file:, vbscript:, and malformed URLs."""
    if not raw_url:
        return False
    # Strip ASCII control characters and whitespace
    cleaned = "".join(ch for ch in raw_url.strip() if ord(ch) > 31)
    if not cleaned:
        return False
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return False

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_LINK_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    return True


def _balance_unclosed_inline_tokens(line: str, is_streaming: bool) -> str:
    """Balance unclosed inline backticks, bold (**), or italic (*) markers
    on the active streaming line so partial tokens render cleanly."""
    if not is_streaming or not line:
        return line

    # 1. Balance single inline backtick if odd count
    backtick_count = line.count("`")
    if backtick_count % 2 == 1:
        # Avoid balancing if the line only ends with a lone backtick that just arrived
        if not line.endswith("`"):
            line = line + "`"
        else:
            # Strip trailing lone backtick marker or leave escaped
            pass

    # 2. Balance bold ** markers if odd number of ** occurrences
    bold_count = line.count("**")
    if bold_count % 2 == 1 and not line.endswith("**"):
        line = line + "**"

    # 3. Balance single * italic markers (excluding **)
    stripped_bold = line.replace("**", "")
    star_count = stripped_bold.count("*")
    if star_count % 2 == 1 and not stripped_bold.endswith("*"):
        line = line + "*"

    return line


def _format_inline(raw_line: str, is_streaming: bool = False) -> str:
    """Escape raw text first and safely apply inline Markdown (code, links, bold, italic).
    Only renderer-generated safe tags are emitted."""
    balanced_line = _balance_unclosed_inline_tokens(raw_line, is_streaming=is_streaming)

    placeholders: dict[str, str] = {}
    counter = 0

    def _store_placeholder(safe_html: str) -> str:
        nonlocal counter
        key = f"\x00MDPH{counter}\x00"
        counter += 1
        placeholders[key] = safe_html
        return key

    # Step 1: Extract inline code spans (`...`) before escaping the rest
    def _replace_inline_code(match: re.Match) -> str:
        code_text = html.escape(match.group(1), quote=True)
        styled = (
            '<code style="background-color:#17253b; color:#7dd3fc; '
            "font-family:'Ubuntu Mono','JetBrains Mono',monospace; "
            'font-size:12.5px; padding:1px 5px; border-radius:4px;">'
            f"{code_text}</code>"
        )
        return _store_placeholder(styled)

    working = _INLINE_CODE_RE.sub(_replace_inline_code, balanced_line)

    # Step 2: Extract Markdown links [label](url) and validate URL protocol
    def _replace_link(match: re.Match) -> str:
        raw_label = match.group(1)
        raw_url = match.group(2).strip()
        safe_label = html.escape(raw_label, quote=True)

        if is_safe_http_url(raw_url):
            safe_url = html.escape(raw_url, quote=True)
            link_html = (
                f'<a href="{safe_url}" '
                'style="color:#38bdf8; text-decoration:underline;">'
                f"{safe_label}</a>"
            )
            return _store_placeholder(link_html)
        else:
            # Unsafe scheme (javascript:, data:, file:, etc.) -> render as inert escaped text
            safe_raw_url = html.escape(raw_url, quote=True)
            inert_html = (
                f'<span style="color:#94a3b8;">[{safe_label}]({safe_raw_url})</span>'
            )
            return _store_placeholder(inert_html)

    working = _LINK_RE.sub(_replace_link, working)

    # Step 3: Escape ALL remaining raw characters so no raw HTML from model survives
    escaped = html.escape(working, quote=True)

    # Step 4: Apply bold and italic on already-escaped text
    escaped = _BOLD_RE.sub(
        r'<b style="color:#f8fafc; font-weight:700;">\1</b>',
        escaped,
    )
    escaped = _ITALIC_RE.sub(
        r'<i style="color:#cbd5e1;">\1</i>',
        escaped,
    )

    # Step 5: Restore placeholders
    for key, safe_fragment in placeholders.items():
        escaped = escaped.replace(key, safe_fragment)

    return escaped


def _render_code_block(code_text: str, language: str, is_unclosed_stream: bool) -> str:
    """Render a fenced code block as a full-width dark table with language badge."""
    safe_lang = html.escape((language or "code").strip().upper(), quote=True)
    if not safe_lang:
        safe_lang = "CODE"

    status_suffix = (
        ' <span style="color:#38bdf8; font-weight:normal;">• yazılıyor...</span>'
        if is_unclosed_stream
        else ""
    )

    # Escape every character inside the code block
    safe_code = html.escape(code_text, quote=True)
    if not safe_code.strip():
        safe_code = " "

    return (
        '<table width="100%" cellspacing="0" cellpadding="0" '
        'style="margin-top:8px; margin-bottom:8px; border:1px solid #1e3352; '
        'border-radius:8px; background-color:#070b14;">'
        "<tr>"
        '<td style="background-color:#0d1729; color:#7dd3fc; '
        "font-family:'Ubuntu Mono','JetBrains Mono',monospace; "
        'font-size:10.5px; font-weight:bold; padding:5px 10px; '
        'border-bottom:1px solid #1e3352;">'
        f"{safe_lang}{status_suffix}"
        "</td>"
        "</tr>"
        "<tr>"
        '<td style="background-color:#070b14; color:#e2e8f0; '
        "font-family:'Ubuntu Mono','JetBrains Mono',monospace; "
        'font-size:12.5px; padding:10px; line-height:1.45;">'
        f'<pre style="margin:0; white-space:pre-wrap; '
        "font-family:'Ubuntu Mono','JetBrains Mono',monospace;\">"
        f"<code>{safe_code}</code></pre>"
        "</td>"
        "</tr>"
        "</table>"
    )


def render_markdown(text: str, is_streaming: bool = False) -> str:
    """Convert raw Markdown text into safe, styled HTML for QTextBrowser.
    Preserves unclosed fenced code blocks and inline formatting during streaming."""
    if not text:
        return (
            '<div style="color:#e8f4ff; font-size:13.5px; line-height:1.5;"></div>'
        )

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    html_blocks: list[str] = []
    in_code_block = False
    code_lang = ""
    code_lines: list[str] = []

    in_ul = False
    in_ol = False
    paragraph_lines: list[str] = []

    def _flush_paragraph():
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        joined = "<br>".join(paragraph_lines)
        html_blocks.append(
            f'<p style="margin-top:4px; margin-bottom:6px; color:#e8f4ff; '
            f'line-height:1.5;">{joined}</p>'
        )
        paragraph_lines = []

    def _close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            html_blocks.append("</ul>")
            in_ul = False
        if in_ol:
            html_blocks.append("</ol>")
            in_ol = False

    for idx, line in enumerate(lines):
        is_last_line = idx == len(lines) - 1
        stripped = line.strip()

        # Check for fenced code block delimiter (```)
        if stripped.startswith("```"):
            if not in_code_block:
                _flush_paragraph()
                _close_lists()
                in_code_block = True
                code_lang = stripped[3:].strip()
                code_lines = []
            else:
                # Closing fence
                html_blocks.append(
                    _render_code_block(
                        "\n".join(code_lines),
                        code_lang,
                        is_unclosed_stream=False,
                    )
                )
                in_code_block = False
                code_lang = ""
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # Empty line separates paragraphs and lists
        if not stripped:
            _flush_paragraph()
            _close_lists()
            continue

        # Horizontal rule
        if _HR_RE.match(line):
            _flush_paragraph()
            _close_lists()
            html_blocks.append(
                '<hr style="border:none; border-top:1px solid #1e3150; margin:8px 0;">'
            )
            continue

        # Headings (# to ####)
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            _flush_paragraph()
            _close_lists()
            level = len(heading_match.group(1))
            content = _format_inline(
                heading_match.group(2),
                is_streaming=(is_streaming and is_last_line),
            )
            sizes = {1: "17px", 2: "15.5px", 3: "14.5px", 4: "13.5px"}
            tag = f"h{min(level + 2, 5)}"
            font_size = sizes.get(level, "14px")
            html_blocks.append(
                f'<{tag} style="color:#7dd3fc; font-size:{font_size}; '
                f'font-weight:bold; margin-top:8px; margin-bottom:4px;">'
                f"{content}</{tag}>"
            )
            continue

        # Blockquote (> ...)
        bq_match = _BLOCKQUOTE_RE.match(line)
        if bq_match:
            _flush_paragraph()
            _close_lists()
            content = _format_inline(
                bq_match.group(1),
                is_streaming=(is_streaming and is_last_line),
            )
            html_blocks.append(
                '<blockquote style="margin:6px 0 6px 8px; padding-left:10px; '
                'border-left:3px solid #38bdf8; color:#cbd5e1;">'
                f"{content}</blockquote>"
            )
            continue

        # Unordered list (- or *)
        ul_match = _UL_RE.match(line)
        if ul_match:
            _flush_paragraph()
            if in_ol:
                html_blocks.append("</ol>")
                in_ol = False
            if not in_ul:
                html_blocks.append(
                    '<ul style="margin-top:4px; margin-bottom:6px; padding-left:18px;">'
                )
                in_ul = True
            item_html = _format_inline(
                ul_match.group(1),
                is_streaming=(is_streaming and is_last_line),
            )
            html_blocks.append(
                f'<li style="margin-bottom:3px; color:#e8f4ff;">{item_html}</li>'
            )
            continue

        # Ordered list (1. ...)
        ol_match = _OL_RE.match(line)
        if ol_match:
            _flush_paragraph()
            if in_ul:
                html_blocks.append("</ul>")
                in_ul = False
            if not in_ol:
                html_blocks.append(
                    '<ol style="margin-top:4px; margin-bottom:6px; padding-left:18px;">'
                )
                in_ol = True
            item_html = _format_inline(
                ol_match.group(2),
                is_streaming=(is_streaming and is_last_line),
            )
            html_blocks.append(
                f'<li style="margin-bottom:3px; color:#e8f4ff;">{item_html}</li>'
            )
            continue

        # Regular paragraph line
        _close_lists()
        paragraph_lines.append(
            _format_inline(
                line,
                is_streaming=(is_streaming and is_last_line),
            )
        )

    # Flush remaining open structures at end of buffer
    _flush_paragraph()
    _close_lists()

    # If a fenced code block is still open (streaming in progress or unclosed fence),
    # preserve and render it cleanly as an active code block!
    if in_code_block:
        html_blocks.append(
            _render_code_block(
                "\n".join(code_lines),
                code_lang,
                is_unclosed_stream=is_streaming,
            )
        )

    body_html = "".join(html_blocks)
    return (
        '<div style="color:#e8f4ff; font-size:13.5px; line-height:1.5;">'
        f"{body_html}</div>"
    )
