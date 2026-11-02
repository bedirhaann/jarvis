"""JARVIS Stage 8 — Turkish Text Normalization, FTS Query Builder, Secret Filter,
Prompt-Injection Sanitizer, and Candidate Suggestion Extractor."""

from __future__ import annotations

import hashlib
import re


class MemorySecretError(ValueError):
    """Raised when an attempt is made to store a secret/credential in memory."""


_TR_ASCII_MAP = str.maketrans({
    "ı": "i",
    "İ": "i",
    "I": "i",
    "ğ": "g",
    "Ğ": "g",
    "ü": "u",
    "Ü": "u",
    "ş": "s",
    "Ş": "s",
    "ö": "o",
    "Ö": "o",
    "ç": "c",
    "Ç": "c",
    "â": "a",
    "Â": "a",
    "î": "i",
    "Î": "i",
    "û": "u",
    "Û": "u",
})

_TURKISH_STOPWORDS = {
    "ve",
    "ile",
    "bir",
    "bu",
    "su",
    "o",
    "da",
    "de",
    "mi",
    "mu",
    "ne",
    "nasil",
    "neden",
    "icin",
    "gibi",
    "kadar",
    "daha",
    "cok",
    "az",
    "var",
    "yok",
    "olan",
    "olarak",
    "ama",
    "fakat",
    "ancak",
    "veya",
    "ya",
    "ki",
    "ben",
    "sen",
    "biz",
    "siz",
    "onlar",
    "benim",
    "senin",
    "nedir",
    "acaba",
}

# Common Turkish nominal/possessive/case suffixes for stem-prefix matching
_TR_COMMON_SUFFIXES = (
    "larinin",
    "lerinin",
    "larinda",
    "lerinde",
    "lardan",
    "lerden",
    "스",
    "nin",
    "nun",
    "sinin",
    "sunun",
    "sinda",
    "sinde",
    "sindan",
    "sinden",
    "lari",
    "leri",
    "dan",
    "den",
    "tan",
    "ten",
    "nda",
    "nde",
    "ini",
    "unu",
    "ina",
    "una",
    "im",
    "in",
    "un",
    "da",
    "de",
    "ta",
    "te",
    "yi",
    "yu",
    "ya",
    "ye",
    "li",
    "lu",
    "si",
    "su",
)

# Secret / credential detection patterns
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/API Secret Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")),
    ("GitHub Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("GitHub Fine-Grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{15,}\b")),
    ("Private Key Block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "Credential Assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|parola|sifre)\s*[:=]\s*['\"]?[^\s'\"]{6,}"
        ),
    ),
]

# Delimiters and instruction-injection strings stripped from untrusted memory content
_FORBIDDEN_DELIMITERS_RE = re.compile(
    r"(?i)(\[/?JARVIS_MEMORY[^\]]*\]|</?JARVIS_MEMORY[^>]*>|</?system[^>]*>|"
    r"</?instruction[^>]*>|\[/?SYSTEM[^\]]*\]|\[/?INST[^\]]*\])"
)

_INSTRUCTION_OVERRIDE_RE = re.compile(
    r"(?i)\b(ignore\s+(?:all\s+)?previous\s+instructions|"
    r"disregard\s+(?:all\s+)?system\s+instructions|"
    r"[öo]nceki\s+(?:t[üu]m\s+)?talimatlar[ıi]\s+un[uü]t|"
    r"sistem\s+talimatlar[ıi]n[ıi]\s+yoksay)\b"
)


def normalize_tr(text: str) -> str:
    """Normalize Turkish characters to lowercase ASCII tokens for consistent search."""
    if not text:
        return ""
    lowered = text.translate(_TR_ASCII_MAP).lower()
    cleaned = re.sub(r"[^a-z0-9\s_\-]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def compute_content_hash(text: str) -> str:
    """Compute SHA-256 hash of normalized content for exact de-duplication."""
    norm = normalize_tr(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def sanitize_memory_content(
    raw_text: str,
    max_length: int = 600,
    max_len: int | None = None,
) -> str:
    """Sanitize memory content so it can never break out of [JARVIS_MEMORY] blocks
    or spoof system/prompt-injection directives."""
    if not raw_text:
        return ""
    limit = max_len if max_len is not None else max_length
    cleaned = _FORBIDDEN_DELIMITERS_RE.sub("", raw_text)
    cleaned = _INSTRUCTION_OVERRIDE_RE.sub("[filtrelendi]", cleaned)
    # Collapse newlines and control characters into single spaces
    cleaned = re.sub(r"[\r\n\t\x00-\x1f]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def contains_secret(text: str) -> tuple[bool, str | None]:
    """Check whether text contains an API key, password, or private key."""
    if not text:
        return False, None
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return True, label
    return False, None


def _stem_turkish_token(token: str) -> str:
    """Produce a conservative stem for Turkish words so inflected forms
    (e.g., 'projesinin' -> 'proje') match via FTS5 prefix queries."""
    if len(token) <= 4:
        return token
    for suffix in _TR_COMMON_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    if len(token) >= 7:
        return token[:5]
    return token


def build_fts_query(raw_query: str, max_terms: int = 8) -> str:
    """Build a safe SQLite FTS5 query string from user input.
    Combines exact prefix tokens and Turkish-stemmed prefix tokens with OR."""
    norm = normalize_tr(raw_query)
    if not norm:
        return ""

    raw_tokens = [
        tok
        for tok in norm.split()
        if len(tok) >= 2 and tok not in _TURKISH_STOPWORDS
    ]
    if not raw_tokens:
        raw_tokens = [tok for tok in norm.split() if len(tok) >= 2]
    if not raw_tokens:
        return ""

    clauses: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens[:max_terms]:
        safe_tok = re.sub(r"[^a-z0-9]", "", tok)
        if len(safe_tok) < 2:
            continue
        stem = _stem_turkish_token(safe_tok)
        for candidate in (safe_tok, stem):
            if len(candidate) >= 2 and candidate not in seen:
                seen.add(candidate)
                clauses.append(f'"{candidate}"*')

    return " OR ".join(clauses)


# Conservative heuristic patterns for user-confirmed ('pending') memory suggestions
_SUGGESTION_PATTERNS: list[tuple[str, str, int, re.Pattern[str]]] = [
    (
        "global",
        "fact",
        4,
        re.compile(
            r"(?i)\b(benim\s+ad[ıi]m\s+[A-ZÇĞİÖŞÜa-zçğıöşü]{2,}(?:\s+[A-ZÇĞİÖŞÜa-zçğıöşü]{2,})?)"
        ),
    ),
    (
        "global",
        "preference",
        4,
        re.compile(
            r"(?i)\b((?:bana\s+[^\.\!\?]{3,60}\s+(?:diye\s+)?hitap\s+et)|"
            r"(?:yan[ıi]tlar[ıi]n?\s+her\s+zaman\s+[^\.\!\?]{3,80}\s+olsun)|"
            r"(?:tercihim\s+[^\.\!\?]{4,90}))"
        ),
    ),
    (
        "global",
        "instruction",
        4,
        re.compile(r"(?i)\b(bundan\s+sonra\s+[^\.\!\?]{6,120})"),
    ),
    (
        "project",
        "decision",
        4,
        re.compile(
            r"(?i)\b((?:bu\s+projede|projemizde|mimari\s+olarak)\s+[^\.\!\?]{6,120}\s+"
            r"(?:kullan[ıi]yoruz|kullanaca[ğg][ıi]z|tercih\s+ettik|karar\s+verdik))"
        ),
    ),
    (
        "global",
        "fact",
        3,
        re.compile(
            r"(?i)\b(ben\s+bir?\s+[^\.\!\?]{4,80}\s+(?:m[üu]hendisiyim|geli[şs]tiricisiyim|kullan[ıi]c[ıi]s[ıi]y[ıi]m|uzman[ıi]y[ıi]m))"
        ),
    ),
]


def extract_memory_suggestions(
    user_message: str,
    has_active_project: bool = False,
) -> list[tuple[str, str, str, int]]:
    """Extract candidate memories from a user message.
    Returns list of (scope, kind, sanitized_content, importance).
    All returned items MUST be stored with status='pending' and origin='extracted'
    until explicitly confirmed by the user."""
    if not user_message or not user_message.strip():
        return []
    text = user_message.strip()
    if text.startswith("/"):
        return []

    has_secret, _ = contains_secret(text)
    if has_secret:
        return []

    results: list[tuple[str, str, str, int]] = []
    seen_hashes: set[str] = set()

    for default_scope, kind, importance, pattern in _SUGGESTION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        snippet = sanitize_memory_content(match.group(1))
        if len(snippet) < 6:
            continue
        scope = default_scope
        if scope == "project" and not has_active_project:
            scope = "global"
        h = compute_content_hash(snippet)
        if h not in seen_hashes:
            seen_hashes.add(h)
            results.append((scope, kind, snippet, importance))

    return results
