"""Allow-list HTML sanitiser and link inspector for draft bodies (DESIGN.md §Safety).

Draft HTML comes from a language model and leaves the mailbox when ``send_draft``
is confirmed; reply and forward drafts also carry quoted third-party HTML.

Four promises (DESIGN.md §Safety):
1. A body that went through ``sanitise_html`` is exact: its links, their text,
   and mismatch warnings are right because ``sanitise_html`` rebuilt that markup.
2. For any stored body, ``link_hosts`` includes the union of destination hosts
   found across six readings of the raw text (character references: none
   decoded, browser rules, and 3-pass fixed point; whitespace: tab/CR/LF
   deleted and replaced by a space), or ``links_unverified`` is true.
3. Critical notices are ordered first and never cut: unverified and size
   notices, then the singular hosts-omitted and outside-normal-markup notices,
   then mismatch warnings (up to 100, plus a count line and
   ``links_unverified: true`` beyond 100).
4. Cost is bounded: ``sanitise_html`` < 2 s up to 200,000 chars;
   ``inspect_draft_links`` < 5 s up to 2,000,000 chars (above which hosts are
   listed by the raw text scan only with ``links_unverified: true``); no single
   library call holds the GIL for more than ~0.2 s.
"""

from __future__ import annotations

import html
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from html.entities import html5 as _HTML5_ENTITIES
from typing import Any, Iterator
from urllib.parse import unquote, urljoin, urlsplit

MAX_HTML_CHARS = 200_000
MAX_INSPECT_HTML_CHARS = 2_000_000
MAX_STYLE_VALUE_CHARS = 200
MAX_HREF_CHARS = 2048
MAX_TITLE_CHARS = 1000
MAX_PREVIEW_LINKS = 20
MAX_PREVIEW_LINK_TEXT_CHARS = 120
MAX_PREVIEW_HOSTS = 50
MAX_PREVIEW_WARNINGS = 20
MAX_MISMATCH_WARNINGS = 100
MAX_HOST_CHARS = 253
TRUNCATED_HOST_CHARS = 80
MAX_COMBINING_RUN = 30
_CHUNK_SIZE = 65_536

ALLOWED_ELEMENTS: frozenset[str] = frozenset({
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "colgroup", "col",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code", "hr",
    "a", "font",
})

VOID_ELEMENTS: frozenset[str] = frozenset({"br", "hr", "col"})
WRAPPER_ELEMENTS: frozenset[str] = frozenset({"html", "body"})

DROP_CONTENT_ELEMENTS: frozenset[str] = frozenset({
    "script", "style", "head", "title", "meta", "link", "base",
    "object", "embed", "iframe", "frame", "frameset", "applet",
    "noembed", "noframes",
    "form", "input", "button", "select", "option", "textarea",
    "svg", "math", "template", "noscript",
    "audio", "video", "source", "track", "canvas", "map", "area", "img",
})

DROP_VOID_ELEMENTS: frozenset[str] = frozenset({
    "meta", "link", "base", "embed", "frame",
    "input", "source", "track", "area", "img",
})
_ALL_HTML_VOID_ELEMENTS: frozenset[str] = VOID_ELEMENTS | DROP_VOID_ELEMENTS | frozenset({
    "wbr", "param", "keygen", "command", "menuitem", "bgsound", "isindex", "nextid",
})
_FOREIGN_ROOTS: frozenset[str] = frozenset({"svg", "math"})
_RAWTEXT_HTML_TAGS: frozenset[str] = frozenset({
    "script", "style", "title", "textarea",
    "noembed", "noframes", "xmp", "iframe",
})
_HEAD_ELEMENTS: frozenset[str] = frozenset({
    "base", "link", "meta", "title", "style", "script", "noscript", "template",
})

# Implied end-tag and table-structure sets (HTML5 / libxml2 alignment).
_HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_P_CLOSERS: frozenset[str] = frozenset({
    "p", "div", "ul", "ol", "li", "table",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "hr",
})
_P_SCOPE_BARRIERS: tuple[str, ...] = (
    "div", "blockquote", "li", "ul", "ol", "table", "td", "th", "caption",
)
_CELL_SCOPE_BARRIERS: tuple[str, ...] = ("table", "td", "th", "caption")
_TABLE_SUBELEMENTS: frozenset[str] = frozenset({
    "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr", "td", "th",
})
_TABLE_SECTIONS: frozenset[str] = frozenset({"thead", "tbody", "tfoot"})
_TABLE_BARE_CONTAINERS: frozenset[str] = frozenset({
    "table", "colgroup", "thead", "tbody", "tfoot", "tr",
})
# Immediate-parent auto-close rules from libxml2 HTMLparser.c htmlStartClose so
# re-parsing our serialised output with lxml.html produces the identical tree.
_LIBXML_IMMEDIATE_CLOSERS: dict[str, frozenset[str]] = {
    "table": frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}),
    "li": frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "li"}),
    "ul": frozenset({"p", "ol", "pre"}),
    "ol": frozenset({"p", "ul"}),
    "pre": frozenset({"p", "ul"}),
    "p": frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6"}),
    "div": frozenset({"p"}),
    "blockquote": frozenset({"p"}),
    "hr": frozenset({"p"}),
    "h1": frozenset({"p"}),
    "h2": frozenset({"p"}),
    "h3": frozenset({"p"}),
    "h4": frozenset({"p"}),
    "h5": frozenset({"p"}),
    "h6": frozenset({"p"}),
    "a": frozenset({"a"}),
}

GLOBAL_ATTRS: frozenset[str] = frozenset({"dir", "lang", "align", "style"})
ELEMENT_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "td": frozenset({"colspan", "rowspan", "width", "height"}),
    "th": frozenset({"colspan", "rowspan", "width", "height"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "width"}),
    "font": frozenset({"face", "size", "color"}),
    "ol": frozenset({"start", "type"}),
    "col": frozenset({"span", "width"}),
}
URL_INSPECT_ATTRS: frozenset[str] = frozenset({
    "href", "xlink:href", "action", "formaction",
    "src", "srcset", "background", "poster", "data", "cite",
})

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})
ALLOWED_DIR_VALUES: frozenset[str] = frozenset({"ltr", "rtl", "auto"})
ALLOWED_ALIGN_VALUES: frozenset[str] = frozenset({"left", "right", "center", "justify"})
ALLOWED_OL_TYPES: frozenset[str] = frozenset({"1", "a", "A", "i", "I"})
ALLOWED_UNICODE_BIDI: frozenset[str] = frozenset({"normal", "embed", "isolate", "plaintext"})
ALLOWED_FONT_SIZE_KEYWORDS: frozenset[str] = frozenset({
    "xx-small", "x-small", "small", "medium", "large",
    "x-large", "xx-large", "xxx-large", "smaller", "larger",
    "inherit", "initial",
})

ALLOWED_STYLE_PROPERTIES: frozenset[str] = frozenset({
    "direction",
    "unicode-bidi",
    "text-align",
    "vertical-align",
    "line-height",
    "white-space",
    "font-family",
    "font-size",
    "font-weight",
    "font-style",
    "text-decoration",
    "color",
    "background-color",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-width", "border-top-width", "border-right-width",
    "border-bottom-width", "border-left-width",
    "border-style", "border-top-style", "border-right-style",
    "border-bottom-style", "border-left-style",
    "border-color", "border-top-color", "border-right-color",
    "border-bottom-color", "border-left-color",
    "border-collapse",
    "width",
    "height",
    "list-style-type",
})

_STYLE_PUNCT: frozenset[str] = frozenset(" #%,.()'\"-+!")
_STYLE_FORBIDDEN_WORDS: tuple[str, ...] = (
    "url(", "expression", "javascript", "vbscript", "behavior", "-moz-binding",
)
_STYLE_FORBIDDEN_CHARS: tuple[str, ...] = ("@", "\\", "<", ">", "/*")
_CSS_WS_RE = re.compile(r"[ \t\n\r\x0c]+")
_FONT_SIZE_NUM_RE = re.compile(
    r"^([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*([a-zA-Z%]*)(?:\s*!important)?$",
    re.IGNORECASE,
)

_LANG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_DIM_RE = re.compile(r"^\d{1,5}(?:\.\d{1,2})?(?:%|px)?$", re.IGNORECASE)
_NONNEG_INT_RE = re.compile(r"^\d{1,4}$")
_SIGNED_INT_RE = re.compile(r"^-?\d{1,6}$")
_FONT_SIZE_ATTR_RE = re.compile(r"^[+-]?[1-7]$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_NAMED_COLOR_RE = re.compile(r"^[A-Za-z]{2,30}$")

# Illegal XML 1.0 C0 controls (preserving \t, \n, \r), UTF-16 surrogates,
# XML noncharacters U+FFFE/U+FFFF, and bidi overrides U+202D (LRO) / U+202E (RLO).
_TEXT_STRIP_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202d\u202e\ud800-\udfff\ufffe\uffff]"
)

# Named HTML5 entity lookup with and without trailing ';'.
_HTML5_NAMED_NOSEMI: dict[str, str] = {}
for _ent_k, _ent_v in _HTML5_ENTITIES.items():
    _bare_k = _ent_k[:-1] if _ent_k.endswith(";") else _ent_k
    if _bare_k and (_bare_k not in _HTML5_NAMED_NOSEMI or _ent_k.endswith(";")):
        _HTML5_NAMED_NOSEMI[_bare_k] = _ent_v
_MAX_NAMED_ENT_LEN = max(len(_k) for _k in _HTML5_NAMED_NOSEMI)


def _ranges_to_regex_class(codes: list[int]) -> str:
    if not codes:
        return ""
    codes.sort()
    parts: list[str] = []
    start = prev = codes[0]
    for c in codes[1:]:
        if c == prev + 1:
            prev = c
        else:
            parts.append(
                re.escape(chr(start))
                if start == prev
                else f"{re.escape(chr(start))}-{re.escape(chr(prev))}"
            )
            start = prev = c
    parts.append(
        re.escape(chr(start))
        if start == prev
        else f"{re.escape(chr(start))}-{re.escape(chr(prev))}"
    )
    return "".join(parts)


def _build_unicode_regexes() -> tuple[re.Pattern[str], re.Pattern[str]]:
    comb_codes: list[int] = []
    ign_codes: list[int] = list(range(0x00, 0x20)) + list(range(0x7F, 0xA0)) + [
        0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0,
    ]
    for cp in (*range(0x00A0, 0x20000), *range(0xE0000, 0xE1000)):
        if 0xD800 <= cp <= 0xDFFF:
            ign_codes.append(cp)
            continue
        ch = chr(cp)
        cat = unicodedata.category(ch)
        if cat.startswith("M") or unicodedata.combining(ch) != 0:
            comb_codes.append(cp)
        if (
            (cp & 0xFFFF) in (0xFFFE, 0xFFFF)
            or 0xFDD0 <= cp <= 0xFDEF
            or cat in ("Cc", "Cf", "Cs", "Co", "Cn")
        ):
            ign_codes.append(cp)
    for plane in range(2, 17):
        ign_codes.append((plane << 16) | 0xFFFE)
        ign_codes.append((plane << 16) | 0xFFFF)

    comb_cls = _ranges_to_regex_class(comb_codes)
    ign_cls = _ranges_to_regex_class(list(set(ign_codes))) + r"\U000f0000-\U0010ffff"
    return (
        re.compile(f"[{comb_cls}]{{{MAX_COMBINING_RUN + 1},}}"),
        re.compile(f"[{ign_cls}]+"),
    )


_COMBINING_OVER30_RE, _IGNORABLE_SPAN_RE = _build_unicode_regexes()

# Linear-time tokenizer and raw-scan patterns (zero nested quantifiers).
_MARKUP_OPEN_RE = re.compile(r"<[A-Za-z/?!]")
_COMMENT_END_RE = re.compile(r"--!?>")
_ATTR_CHARREF_RE = re.compile(
    r"&(?:#(?:[0-9]+|[xX][0-9a-fA-F]+);?|[A-Za-z][A-Za-z0-9]{1,31};)"
)
_SCAN_CHARREF_RE = re.compile(
    r"&(?:#(?:[0-9]+|[xX][0-9a-fA-F]+);?|[A-Za-z][A-Za-z0-9]{1,31};?)"
)
_SCAN_URL_RE = re.compile(
    r"(?:(https?|ftps?|wss?|file):([\\/]*)|(?<![\\/])([\\/]{2,}))"
    r"([^\s/\\?#\"'`<>)]+)",
    re.IGNORECASE,
)
_SCAN_USERINFO_URL_RE = re.compile(
    r"(?:(?:https?|ftps?|wss?|file):[\\/]+|(?<![\\/])[\\/]{2,})([^\s/\\?#]+)",
    re.IGNORECASE,
)
_SCAN_MAILTO_RE = re.compile(
    r"mailto:([^\s\"'`<>)]+)",
    re.IGNORECASE,
)
_MAILTO_ADDR_SPLIT_RE = re.compile(r"[,;\s]+")
_MAILTO_QUERY_SPLIT_RE = re.compile(r"[&,;=+\s]+")
_TAB_CR_LF_DELETE = str.maketrans("", "", "\x00\t\r\n")
_TAB_CR_LF_SPACE = str.maketrans("\x00\t\r\n", "    ")
_ASCII_CTRL_STRIP_MAP = str.maketrans(
    "", "", "".join(chr(c) for c in (*range(0x20), 0x7F))
)
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]+|0b[01]+|0o[0-7]+|[0-9]+)$")

_ASCII_WS = " \t\n\r\x0c"
_TAG_NAME_STOP = " \t\n\r\x0c/>"

_TEXT_BREAK_TAGS: frozenset[str] = frozenset({
    "p", "div", "tr", "li", "table", "blockquote", "pre", "hr",
    "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
})

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_EXPLICIT_URL_TOKEN_RE = re.compile(r"(?:https?://|mailto:)[^\s<>\"']+", re.IGNORECASE)
_CANDIDATE_TOKEN_RE = re.compile(r"[\w+\-][\w.%+\-@:]*")

# Non-TLD document extensions (excluding ICANN TLDs like .zip and .mov).
_NON_TLD_EXTS: frozenset[str] = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "rtf",
    "tar", "gz", "7z", "rar", "png", "jpg", "jpeg", "gif", "svg",
    "mp3", "mp4", "wav", "json", "xml", "yaml", "yml", "log", "msg", "eml",
    "ics", "p7m", "p7s", "bin",
})

_LOOKALIKE_SEP_MAP = str.maketrans({
    "\u3002": ".",
    "\uff0e": ".",
    "\uff61": ".",
    "\u2024": ".",
    "\u2044": "/",
    "\u2215": "/",
    "\uff0f": "/",
    "\u29f8": "/",
})
_SUSPICIOUS_HOST_CHARS: frozenset[str] = frozenset(
    "%\u3002\uff0e\uff61\u2024\u2044\u2215\uff0f\u29f8"
)

_TIER5_KIND_LABELS: dict[str, str] = {
    "empty_text": "empty-link",
    "numeric_host": "numeric-host",
    "overlong_host": "overlong-host",
    "idn_host": "non-ASCII/punycode-host",
    "suspicious_host": "suspicious-host",
    "userinfo": "embedded-userinfo",
    "malformed_url": "malformed-link",
    "nonstandard_scheme": "non-standard-scheme",
    "non_anchor_active": "non-anchor-URL",
    "bidi_override": "bidirectional-override",
}


class HTMLValidationError(ValueError):
    """Raised when an HTML body fails input size, structure, or visible-text checks."""


@dataclass(frozen=True)
class SanitiseResult:
    html: str
    text: str
    sanitised: dict[str, Any] | None


@dataclass
class _Node:
    tag: str
    attrs: list[tuple[str, str]] = field(default_factory=list)
    children: list[_Node | str] = field(default_factory=list)
    had_removed_link: bool = False
    unwrap_anchor: bool = False


# --- Character, combining-mark & entity helpers -------------------------------


def _is_combining_char(ch: str) -> bool:
    if ord(ch) < 0x0300:
        return False
    return unicodedata.combining(ch) != 0 or unicodedata.category(ch).startswith("M")


def _trim_combining_match(match: re.Match[str]) -> str:
    return match.group(0)[:MAX_COMBINING_RUN]


def _cap_combining_marks(text: str) -> str:
    """Cut any run of > 30 combining marks to 30 (Unicode UAX #15 stream-safe
    limit) in 64 KiB slices so no call can go quadratic or hold the GIL."""
    if not text or text.isascii():
        return text
    n = len(text)
    if n <= _CHUNK_SIZE:
        if _COMBINING_OVER30_RE.search(text) is None:
            return text
        return _COMBINING_OVER30_RE.sub(_trim_combining_match, text)

    out_chunks: list[str] = []
    pos = 0
    trailing_comb = 0
    while pos < n:
        end = min(n, pos + _CHUNK_SIZE)
        chunk = text[pos:end]
        pos = end
        if chunk.isascii():
            out_chunks.append(chunk)
            trailing_comb = 0
            continue
        if trailing_comb > 0:
            prefixed = ("\u0301" * trailing_comb) + chunk
            trimmed = _COMBINING_OVER30_RE.sub(_trim_combining_match, prefixed)
            cleaned_chunk = trimmed[trailing_comb:]
        elif _COMBINING_OVER30_RE.search(chunk) is not None:
            cleaned_chunk = _COMBINING_OVER30_RE.sub(_trim_combining_match, chunk)
        else:
            cleaned_chunk = chunk
        if cleaned_chunk:
            out_chunks.append(cleaned_chunk)
        # Update trailing combining-mark count (bounded to MAX_COMBINING_RUN).
        tc = 0
        for idx in range(len(cleaned_chunk) - 1, -1, -1):
            if _is_combining_char(cleaned_chunk[idx]):
                tc += 1
                if tc >= MAX_COMBINING_RUN:
                    break
            else:
                break
        if len(cleaned_chunk) < MAX_COMBINING_RUN and tc == len(cleaned_chunk):
            trailing_comb = min(MAX_COMBINING_RUN, trailing_comb + tc)
        else:
            trailing_comb = tc
    return "".join(out_chunks)


def _is_invisible_or_ignorable(ch: str) -> bool:
    """True for Unicode Default_Ignorable_Code_Point, blank fillers, controls,
    bidi formatting, variation selectors, surrogates, and noncharacters."""
    code = ord(ch)
    if (code & 0xFFFF) in (0xFFFE, 0xFFFF) or 0xFDD0 <= code <= 0xFDEF:
        return True
    if code in (
        0x00AD, 0x034F, 0x061C, 0x115F, 0x1160,
        0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0,
    ):
        return True
    if (
        0x180B <= code <= 0x180F
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
    ):
        return True
    return unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn")


def _is_visible_char(ch: str) -> bool:
    return not ch.isspace() and not _is_invisible_or_ignorable(ch)


def _clean_text(data: str) -> str:
    if not data:
        return data
    if data.isascii():
        return _TEXT_STRIP_RE.sub("", data)
    return _cap_combining_marks(_TEXT_STRIP_RE.sub("", data))


def _replace_attr_charref(match: re.Match[str]) -> str:
    token = match.group(0)
    if token[1] == "#":
        return html.unescape(token if token.endswith(";") else token + ";")
    resolved = _HTML5_ENTITIES.get(token[1:])
    return resolved if resolved is not None else token


def _decode_attr_entities(raw: str) -> str:
    """Decode numeric character references (with or without ``;``) and
    semicolon-terminated named character references so query parameters like
    ``&region=sa&copy=1`` stay verbatim."""
    if "&" not in raw:
        return raw
    return _ATTR_CHARREF_RE.sub(_replace_attr_charref, raw)


def _replace_scan_charref(match: re.Match[str]) -> str:
    token = match.group(0)
    if token[1] == "#":
        return html.unescape(token if token.endswith(";") else token + ";")
    has_semi = token.endswith(";")
    body = token[1:-1] if has_semi else token[1:]
    if has_semi:
        exact = _HTML5_ENTITIES.get(token[1:])
        if exact is not None:
            return exact
        no_semi = _HTML5_NAMED_NOSEMI.get(body)
        if no_semi is not None:
            return no_semi
    tail_semi = ";" if has_semi else ""
    max_end = min(len(body), _MAX_NAMED_ENT_LEN)
    for end in range(max_end, 1, -1):
        hit = _HTML5_NAMED_NOSEMI.get(body[:end])
        if hit is not None:
            return hit + body[end:] + tail_semi
    return token


def _decode_scan_entities(raw: str, max_passes: int = 3) -> str:
    """Decode numeric and named character references with and without ``;``,
    repeating until unchanged or ``max_passes`` passes are done."""
    cur = raw
    for _ in range(max_passes):
        if "&" not in cur:
            break
        nxt = _SCAN_CHARREF_RE.sub(_replace_scan_charref, cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def _decode_text_entities(raw: str) -> str:
    if "&" not in raw:
        return raw
    return html.unescape(raw)


def _unescape_fully(value: str, rounds: int = 4) -> str:
    cur = value
    for _ in range(rounds):
        nxt = html.unescape(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


# --- Linear-time, non-raising HTML tokenizer ----------------------------------


Token = tuple[Any, ...]


def _scan_tag_tail(
    s: str, j: int, n: int, *, collect_attrs: bool
) -> tuple[int, list[tuple[str, str | None]], bool, bool]:
    """Scan attributes from ``j`` up to closing ``>`` following HTML5 rules:
    a quote opens a value only directly after ``=`` (with optional ASCII
    whitespace); anywhere else quotes belong to an attribute name or unquoted
    value."""
    attrs: list[tuple[str, str | None]] = []
    self_closing = False
    eof_in_tag = False

    while True:
        while j < n and s[j] in _ASCII_WS:
            j += 1
        if j >= n:
            eof_in_tag = True
            break
        ch = s[j]
        if ch == ">":
            j += 1
            break
        if ch == "/":
            j += 1
            if j < n and s[j] == ">":
                self_closing = True
                j += 1
                break
            continue

        name_start = j
        if ch == "=":
            j += 1
        while j < n and s[j] not in " \t\n\r\x0c/>=":
            j += 1
        attr_name = s[name_start:j].lower() if collect_attrs else ""

        while j < n and s[j] in _ASCII_WS:
            j += 1
        if j < n and s[j] == "=":
            j += 1
            while j < n and s[j] in _ASCII_WS:
                j += 1
            if j >= n:
                eof_in_tag = True
                break
            qch = s[j]
            if qch in ('"', "'"):
                close_q = s.find(qch, j + 1)
                if close_q == -1:
                    eof_in_tag = True
                    j = n
                    break
                if collect_attrs:
                    attrs.append((attr_name, _decode_attr_entities(s[j + 1 : close_q])))
                j = close_q + 1
            elif qch == ">":
                if collect_attrs:
                    attrs.append((attr_name, ""))
            else:
                val_start = j
                while j < n and s[j] not in " \t\n\r\x0c>":
                    j += 1
                if j >= n:
                    eof_in_tag = True
                    break
                if collect_attrs:
                    attrs.append((attr_name, _decode_attr_entities(s[val_start:j])))
        elif collect_attrs:
            attrs.append((attr_name, None))

    return j, attrs, self_closing, eof_in_tag


def _scan_tokens(raw: str, *, drop_cdata: bool = False) -> Iterator[Token]:
    """Single-pass HTML tokenizer. Linear in ``len(raw)``; never raises.

    Yields:
      - ``("text", decoded_str)``
      - ``("start", tag_lower, attrs_list, self_closing_bool)``
      - ``("end", tag_lower)``
    Per HTML5, trailing ``/`` is honoured only on void HTML elements and
    foreign content (``<svg>`` / ``<math>``); on non-void HTML elements such as
    ``<a href="…"/>`` or ``<div/>`` it is ignored.
    """
    s = raw.replace("\x00", "") if "\x00" in raw else raw
    n = len(s)
    i = 0
    rawtext_tag: str | None = None
    foreign_depth = 0

    while i < n:
        if rawtext_tag is not None:
            tlen = len(rawtext_tag)
            search_pos = i
            closed = False
            while True:
                close_pos = s.find("</", search_pos)
                if close_pos == -1:
                    break
                after_name = close_pos + 2 + tlen
                if (
                    after_name <= n
                    and s[close_pos + 2 : after_name].lower() == rawtext_tag
                    and (after_name == n or s[after_name] in _TAG_NAME_STOP)
                ):
                    end_j, _, _, eof_end = _scan_tag_tail(
                        s, after_name, n, collect_attrs=False
                    )
                    if eof_end:
                        break
                    if close_pos > i:
                        yield ("text", s[i:close_pos])
                    yield ("end", rawtext_tag)
                    i = end_j
                    rawtext_tag = None
                    closed = True
                    break
                search_pos = close_pos + 2
            if not closed:
                if i < n:
                    yield ("text", s[i:n])
                i = n
                rawtext_tag = None
            continue

        m = _MARKUP_OPEN_RE.search(s, i)
        if m is None:
            yield ("text", _decode_text_entities(s[i:n]))
            break
        lt = m.start()
        if lt > i:
            yield ("text", _decode_text_entities(s[i:lt]))
        i = lt
        nxt = s[i + 1]

        # 1. Comments, DOCTYPE, marked sections, bogus declarations: <!...>
        if nxt == "!":
            if s.startswith("<!--", i):
                if s.startswith("<!-->", i):
                    i += 5
                    continue
                if s.startswith("<!--->", i):
                    i += 6
                    continue
                end_m = _COMMENT_END_RE.search(s, i + 4)
                i = end_m.end() if end_m is not None else n
                continue
            if (drop_cdata or foreign_depth > 0) and s[i : i + 9].upper() == "<![CDATA[":
                end_cd = s.find("]]>", i + 9)
                i = end_cd + 3 if end_cd != -1 else n
                continue
            gt = s.find(">", i + 2)
            i = gt + 1 if gt != -1 else n
            continue

        # 2. Processing instructions / XML declarations: <?...>
        if nxt == "?":
            gt = s.find(">", i + 2)
            i = gt + 1 if gt != -1 else n
            continue

        # 3. End tags: </...>
        if nxt == "/":
            if i + 2 >= n:
                yield ("text", "</")
                i = n
                break
            c2 = s[i + 2]
            if not ("A" <= c2 <= "Z" or "a" <= c2 <= "z"):
                if c2 == ">":
                    i += 3
                else:
                    gt = s.find(">", i + 2)
                    i = gt + 1 if gt != -1 else n
                continue
            j = i + 2
            while j < n and s[j] not in _TAG_NAME_STOP:
                j += 1
            end_tag = s[i + 2 : j].lower()
            i, _, _, eof_in_end = _scan_tag_tail(s, j, n, collect_attrs=False)
            if eof_in_end:
                break
            if end_tag in _FOREIGN_ROOTS and foreign_depth > 0:
                foreign_depth -= 1
            yield ("end", end_tag)
            continue

        # 4. Start tags: <[A-Za-z]...
        j = i + 1
        while j < n and s[j] not in _TAG_NAME_STOP:
            j += 1
        tag = s[i + 1 : j].lower()
        i, attrs, raw_self_closing, eof_in_tag = _scan_tag_tail(
            s, j, n, collect_attrs=True
        )
        if eof_in_tag:
            break

        effective_self_closing = raw_self_closing and (
            foreign_depth > 0
            or tag in _FOREIGN_ROOTS
            or tag in _ALL_HTML_VOID_ELEMENTS
        )
        yield ("start", tag, attrs, effective_self_closing)
        if tag in _FOREIGN_ROOTS:
            if not raw_self_closing:
                foreign_depth += 1
        elif foreign_depth == 0:
            if tag == "plaintext":
                if i < n:
                    yield ("text", s[i:n])
                    i = n
            elif tag in _RAWTEXT_HTML_TAGS:
                rawtext_tag = tag


# --- Sanitiser attribute, link & style validation -----------------------------


def _validate_http_host(hostname: str) -> bool:
    host = hostname.rstrip(".")
    if not host or len(host) > MAX_HOST_CHARS:
        return False
    if any(ch.isspace() or ch in _SUSPICIOUS_HOST_CHARS for ch in host):
        return False
    if ":" in host:
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False
    labels = host.split(".")
    if any(not lb or len(lb) > 63 or lb.startswith("-") or lb.endswith("-") for lb in labels):
        return False
    for lb in labels:
        if not lb.isascii() or lb.lower().startswith("xn--"):
            try:
                ascii_lb = lb.encode("idna").decode("ascii")
                ascii_lb.encode("ascii").decode("idna")
            except UnicodeError:
                return False
            if not ascii_lb or len(ascii_lb) > 63:
                return False
        elif not all(ch.isalnum() or ch == "-" for ch in lb):
            return False
    return True


def _clean_href(raw_val: str | None) -> tuple[str | None, int]:
    """Return ``(cleaned_href_or_None, extra_removed_link_parts)``."""
    if raw_val is None:
        return None, 1
    decoded = _unescape_fully(raw_val)
    probe = "".join(
        ch for ch in decoded
        if not ch.isspace() and unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )
    colon = probe.find(":")
    if colon <= 0:
        return None, 1
    probe_scheme = probe[:colon].lower()
    if probe_scheme not in ALLOWED_SCHEMES:
        return None, 1

    candidate = raw_val.strip()
    if not candidate or len(candidate) > MAX_HREF_CHARS:
        return None, 1
    for ch in candidate:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return None, 1
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs"):
            return None, 1

    try:
        parts = urlsplit(candidate)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        _ = parts.port
    except ValueError:
        return None, 1

    if scheme != probe_scheme or scheme not in ALLOWED_SCHEMES:
        return None, 1

    if scheme in ("http", "https"):
        prefix = f"{scheme}://"
        if candidate[: len(prefix)].lower() != prefix:
            return None, 1
        before_query = candidate.split("?", 1)[0].split("#", 1)[0]
        if "\\" in before_query or "@" in parts.netloc:
            return None, 1
        if any(ch.isspace() for ch in parts.netloc):
            return None, 1
        if "[" in parts.netloc or "]" in parts.netloc:
            if not hostname:
                return None, 1
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                return None, 1
        elif not hostname or not _validate_http_host(hostname):
            return None, 1
        return prefix + candidate[len(prefix):], 0

    # mailto: keep only the single address; strip extra recipients (including
    # percent-encoded ',', ';', or CR/LF header injections), ?query and #fragment.
    if candidate[:7].lower() != "mailto:" or candidate[:9].lower() == "mailto://":
        return None, 1
    unquoted_path = unquote(parts.path).strip()
    first_addr, *extra_addrs = re.split(r"[,;\r\n]+", unquoted_path)
    addr = first_addr.strip()
    if not addr or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() or ch in "\\<>?#%"
        for ch in addr
    ):
        return None, 1
    if addr.count("@") != 1:
        return None, 1
    local_p, _, dom_p = addr.partition("@")
    if not local_p or not _validate_http_host(dom_p):
        return None, 1
    had_extra = (
        1
        if (
            extra_addrs
            or parts.query
            or parts.fragment
            or "?" in candidate
            or "#" in candidate
            or addr != parts.path.strip()
        )
        else 0
    )
    return f"mailto:{addr}", had_extra


def _is_valid_font_size(val: str) -> bool:
    low = val.lower()
    if low in ALLOWED_FONT_SIZE_KEYWORDS:
        return True
    m = _FONT_SIZE_NUM_RE.match(val)
    if not m:
        return False
    try:
        num = float(m.group(1))
    except ValueError:
        return False
    if num <= 0:
        return False
    unit = m.group(2).lower()
    if unit in ("px", "pt", ""):
        return num >= 6.0
    if unit in ("em", "rem"):
        return num >= 0.375
    if unit == "%":
        return num >= 37.5
    return False


def _is_safe_style_value(val: str) -> bool:
    if not val or len(val) > MAX_STYLE_VALUE_CHARS:
        return False
    for bad_char in _STYLE_FORBIDDEN_CHARS:
        if bad_char in val:
            return False
    lowered = val.casefold()
    compact = "".join(lowered.split())
    for word in _STYLE_FORBIDDEN_WORDS:
        if word in lowered or word in compact:
            return False

    in_quote: str | None = None
    paren_depth = 0
    for ch in val:
        if not (ch.isalpha() or ch.isdecimal() or ch in _STYLE_PUNCT):
            return False
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth < 0:
                return False
    return in_quote is None and paren_depth == 0


def _clean_style(raw_val: str | None) -> tuple[str | None, bool]:
    """Return ``(serialised_style_or_None, had_anything_removed)``."""
    if raw_val is None:
        return None, True
    stripped_all = raw_val.strip()
    if not stripped_all:
        return None, True

    kept: list[str] = []
    removed_any = False
    for chunk in stripped_all.split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        prop_raw, sep, val_raw = piece.partition(":")
        if not sep:
            removed_any = True
            continue
        prop = prop_raw.strip().lower()
        val = _CSS_WS_RE.sub(" ", val_raw).strip()
        if prop not in ALLOWED_STYLE_PROPERTIES or not _is_safe_style_value(val):
            removed_any = True
            continue
        if prop == "unicode-bidi":
            ub = val.lower()
            if ub not in ALLOWED_UNICODE_BIDI:
                removed_any = True
                continue
            val = ub
        elif prop == "font-size":
            if not _is_valid_font_size(val):
                removed_any = True
                continue
        kept.append(f"{prop}: {val}")

    if not kept:
        return None, True
    return "; ".join(kept), removed_any


def _clean_font_face(val: str) -> str | None:
    cleaned = _CSS_WS_RE.sub(" ", val).strip()
    if not cleaned or len(cleaned) > MAX_STYLE_VALUE_CHARS:
        return None
    if not _is_safe_style_value(cleaned):
        return None
    return cleaned


def _clean_attr_value(tag: str, name: str, raw_val: str | None) -> str | None:
    if raw_val is None:
        return None
    val = raw_val.strip()
    if name == "dir":
        low = val.lower()
        return low if low in ALLOWED_DIR_VALUES else None
    if name == "align":
        low = val.lower()
        return low if low in ALLOWED_ALIGN_VALUES else None
    if name == "lang":
        return val if len(val) <= 35 and _LANG_RE.match(val) else None
    if name == "title":
        val = _cap_combining_marks(val)
        if len(val) > MAX_TITLE_CHARS:
            return None
        for ch in val:
            code = ord(ch)
            if (
                (code < 0x20 and ch not in "\t\n\r")
                or code == 0x7F
                or 0xD800 <= code <= 0xDFFF
                or code in (0x202D, 0x202E, 0xFFFE, 0xFFFF)
            ):
                return None
        return val
    if name in ("colspan", "rowspan"):
        return val if _NONNEG_INT_RE.match(val) and int(val) <= 1000 else None
    if name == "span":
        return val if _NONNEG_INT_RE.match(val) and 1 <= int(val) <= 1000 else None
    if name in ("width", "height"):
        return val.lower() if _DIM_RE.match(val) else None
    if name in ("border", "cellpadding", "cellspacing"):
        return val if _NONNEG_INT_RE.match(val) and int(val) <= 1000 else None
    if name == "face":
        return _clean_font_face(val)
    if name == "size":
        return val if _FONT_SIZE_ATTR_RE.match(val) else None
    if name == "color":
        if _HEX_COLOR_RE.match(val) or _NAMED_COLOR_RE.match(val):
            return val
        return None
    if name == "start":
        return val if _SIGNED_INT_RE.match(val) else None
    if name == "type" and tag == "ol":
        return val if val in ALLOWED_OL_TYPES else None
    return None


def _clean_attributes(
    tag: str, attrs: list[tuple[str, str | None]]
) -> tuple[list[tuple[str, str]], int, int]:
    allowed_for_tag = GLOBAL_ATTRS | ELEMENT_ATTRS.get(tag, frozenset())
    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    removed_attrs = 0
    removed_links = 0

    for raw_name, raw_val in attrs:
        name = (raw_name or "").lower()
        if not name or name not in allowed_for_tag:
            removed_attrs += 1
            continue
        if name in seen:
            if tag == "a" and name == "href":
                url_dup, _ = _clean_href(raw_val)
                if url_dup is None:
                    removed_links += 1
                else:
                    removed_attrs += 1
            else:
                removed_attrs += 1
            continue
        seen.add(name)

        if tag == "a" and name == "href":
            clean_url, stripped_parts = _clean_href(raw_val)
            removed_links += stripped_parts
            if clean_url is not None:
                kept.append(("href", clean_url))
            continue

        if name == "style":
            clean_css, style_trimmed = _clean_style(raw_val)
            if style_trimmed:
                removed_attrs += 1
            if clean_css is not None:
                kept.append(("style", clean_css))
            continue

        clean_val = _clean_attr_value(tag, name, raw_val)
        if clean_val is None:
            removed_attrs += 1
        else:
            kept.append((name, clean_val))

    return kept, removed_attrs, removed_links


# --- O(1)-amortised tree builder with HTML5/libxml2 implied end tags ----------


class _SanitisingBuilder:
    def __init__(self) -> None:
        self.root = _Node(tag="")
        self.open_stack: list[_Node] = [self.root]
        self.tag_positions: dict[str, list[int]] = {}
        self.table_stack: list[int] = []
        self.drop_stack: list[str] = []
        self.drop_counts: dict[str, int] = {}
        self.in_head: bool = False
        self.html_attrs: dict[str, str] = {}
        self.body_attrs: dict[str, str] = {}
        self.saw_html: bool = False
        self.saw_body: bool = False
        self.removed_elements: set[str] = set()
        self.removed_attributes: int = 0
        self.removed_links: int = 0

    def _push_drop(self, tag: str) -> None:
        self.drop_stack.append(tag)
        self.drop_counts[tag] = self.drop_counts.get(tag, 0) + 1

    def _last_pos(self, tag: str) -> int:
        positions = self.tag_positions.get(tag)
        return positions[-1] if positions else 0

    def _max_pos(self, tags: tuple[str, ...]) -> int:
        best = 0
        for tg in tags:
            pos = self._last_pos(tg)
            if pos > best:
                best = pos
        return best

    def _push_node(self, node: _Node) -> None:
        self.open_stack[-1].children.append(node)
        if node.tag not in VOID_ELEMENTS:
            idx = len(self.open_stack)
            self.open_stack.append(node)
            self.tag_positions.setdefault(node.tag, []).append(idx)
            if node.tag == "table":
                self.table_stack.append(idx)

    def _close_to(self, target_idx: int) -> None:
        """Pop ``open_stack`` down to ``target_idx`` (inclusive) in O(popped)."""
        while len(self.open_stack) > target_idx:
            popped = self.open_stack.pop()
            self.tag_positions[popped.tag].pop()
            if popped.tag == "table" and self.table_stack:
                self.table_stack.pop()

    def _close_above(self, floor_idx: int) -> None:
        self._close_to(floor_idx + 1)

    def _ensure_flow_parent_in_table(self) -> None:
        """When flow content or non-whitespace text appears directly inside
        ``table`` / ``colgroup`` / ``thead`` / ``tbody`` / ``tfoot`` / ``tr``,
        open implicit ``<tr><td>`` so the tree matches browser/libxml2 parsing."""
        top_tag = self.open_stack[-1].tag
        if top_tag not in _TABLE_BARE_CONTAINERS:
            return
        if top_tag == "colgroup":
            self._close_to(len(self.open_stack) - 1)
            top_tag = self.open_stack[-1].tag
        if top_tag in ("table", "thead", "tbody", "tfoot"):
            self._push_node(_Node(tag="tr"))
            top_tag = "tr"
        if top_tag == "tr":
            self._push_node(_Node(tag="td"))

    def _handle_table_subelement(
        self, tag: str, clean_attrs: list[tuple[str, str]]
    ) -> None:
        tbl_idx = self.table_stack[-1]
        if tag in ("caption", "colgroup"):
            self._close_above(tbl_idx)
            self._push_node(_Node(tag=tag, attrs=clean_attrs))
            return
        if tag == "col":
            cg_pos = self._last_pos("colgroup")
            if cg_pos > tbl_idx:
                self._close_above(cg_pos)
            else:
                self._close_above(tbl_idx)
                self._push_node(_Node(tag="colgroup"))
            self._push_node(_Node(tag="col", attrs=clean_attrs))
            return
        if tag in _TABLE_SECTIONS:
            self._close_above(tbl_idx)
            self._push_node(_Node(tag=tag, attrs=clean_attrs))
            return
        if tag == "tr":
            sec_pos = max(
                self._last_pos("thead"),
                self._last_pos("tbody"),
                self._last_pos("tfoot"),
            )
            if sec_pos > tbl_idx:
                self._close_above(sec_pos)
            else:
                self._close_above(tbl_idx)
            self._push_node(_Node(tag="tr", attrs=clean_attrs))
            return
        # tag in ("td", "th")
        tr_pos = self._last_pos("tr")
        if tr_pos > tbl_idx:
            self._close_above(tr_pos)
        else:
            sec_pos = max(
                self._last_pos("thead"),
                self._last_pos("tbody"),
                self._last_pos("tfoot"),
            )
            if sec_pos > tbl_idx:
                self._close_above(sec_pos)
            else:
                self._close_above(tbl_idx)
            self._push_node(_Node(tag="tr"))
        self._push_node(_Node(tag=tag, attrs=clean_attrs))

    def _apply_implied_end_tags(self, tag: str) -> None:
        cell_floor = self._max_pos(_CELL_SCOPE_BARRIERS)

        if tag == "a":
            a_positions = self.tag_positions.get("a")
            if a_positions:
                if a_positions[-1] > cell_floor:
                    self._close_to(a_positions[-1])
                for apos in a_positions:
                    if 0 < apos <= cell_floor:
                        self.open_stack[apos].unwrap_anchor = True

        if tag in _P_CLOSERS:
            p_floor = self._max_pos(_P_SCOPE_BARRIERS)
            p_pos = self._last_pos("p")
            if p_pos > p_floor:
                self._close_to(p_pos)
            for htag in _HEADING_TAGS:
                h_pos = self._last_pos(htag)
                if h_pos > p_floor:
                    self._close_to(h_pos)

        if tag == "li":
            list_floor = max(cell_floor, self._last_pos("ul"), self._last_pos("ol"))
            li_pos = self._last_pos("li")
            if li_pos > list_floor:
                self._close_to(li_pos)

        libxml_closers = _LIBXML_IMMEDIATE_CLOSERS.get(tag)
        if libxml_closers is not None:
            while self.open_stack[-1].tag in libxml_closers:
                self._close_to(len(self.open_stack) - 1)

    def _record_wrapper_attrs(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "html":
            if self.saw_html:
                self.removed_attributes += len(attrs)
                return
            self.saw_html = True
            target = self.html_attrs
        else:
            if self.saw_body:
                self.removed_attributes += len(attrs)
                return
            self.saw_body = True
            target = self.body_attrs
        clean_attrs, attr_rem, _ = _clean_attributes("body", attrs)
        self.removed_attributes += attr_rem
        for k, v in clean_attrs:
            target[k] = v

    def on_start(
        self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool
    ) -> None:
        if self.drop_stack:
            if (
                tag in DROP_CONTENT_ELEMENTS
                and tag not in DROP_VOID_ELEMENTS
                and not self_closing
                and tag != "head"
            ):
                self._push_drop(tag)
            return

        if self.in_head:
            if tag in _HEAD_ELEMENTS:
                if tag not in DROP_VOID_ELEMENTS and not self_closing:
                    self._push_drop(tag)
                return
            # Omitted </head>: any non-head element (<body>, <p>, <div>, …) closes <head>.
            self.in_head = False

        if tag in WRAPPER_ELEMENTS:
            self._record_wrapper_attrs(tag, attrs)
            return

        if tag == "head":
            self.removed_elements.add("head")
            if not self_closing:
                self.in_head = True
            return

        if tag in DROP_CONTENT_ELEMENTS:
            self.removed_elements.add(tag)
            if tag not in DROP_VOID_ELEMENTS and not self_closing:
                self._push_drop(tag)
            return

        if tag not in ALLOWED_ELEMENTS:
            self.removed_elements.add(tag)
            return

        if tag in _TABLE_SUBELEMENTS:
            if not self.table_stack:
                self.removed_elements.add(tag)
                return
            clean_attrs, attr_rem, link_rem = _clean_attributes(tag, attrs)
            self.removed_attributes += attr_rem
            self.removed_links += link_rem
            self._handle_table_subelement(tag, clean_attrs)
            return

        self._ensure_flow_parent_in_table()
        self._apply_implied_end_tags(tag)
        clean_attrs, attr_rem, link_rem = _clean_attributes(tag, attrs)
        self.removed_attributes += attr_rem
        self.removed_links += link_rem
        node = _Node(
            tag=tag,
            attrs=clean_attrs,
            had_removed_link=(tag == "a" and link_rem > 0 and not any(k == "href" for k, _ in clean_attrs)),
        )
        self._push_node(node)

    def on_end(self, tag: str) -> None:
        if self.drop_stack:
            if self.drop_counts.get(tag, 0) <= 0:
                return
            while self.drop_stack:
                popped = self.drop_stack.pop()
                self.drop_counts[popped] -= 1
                if popped == tag:
                    return
            return

        if tag == "head":
            self.in_head = False
            return

        if (
            tag in WRAPPER_ELEMENTS
            or tag in DROP_CONTENT_ELEMENTS
            or tag not in ALLOWED_ELEMENTS
            or tag in VOID_ELEMENTS
        ):
            return

        pos = self._last_pos(tag)
        if pos <= 0:
            return

        if tag == "table":
            if self.table_stack and pos == self.table_stack[-1]:
                self._close_to(pos)
            return

        if tag in _TABLE_SUBELEMENTS:
            if self.table_stack and pos > self.table_stack[-1]:
                self._close_to(pos)
            return

        floor = self._max_pos(_CELL_SCOPE_BARRIERS)
        if tag == "li":
            floor = max(floor, self._last_pos("ul"), self._last_pos("ol"))
        if pos > floor:
            self._close_to(pos)

    def on_text(self, data: str) -> None:
        if self.drop_stack:
            return
        cleaned = _clean_text(data)
        if not cleaned:
            return
        if self.in_head:
            if not cleaned.strip():
                return
            self.in_head = False
        if self.open_stack[-1].tag in _TABLE_BARE_CONTAINERS:
            if not cleaned.strip():
                return
            self._ensure_flow_parent_in_table()
        # Append without in-place str += so mixed UCS-4 / UCS-1 runs stay O(N).
        self.open_stack[-1].children.append(cleaned)


def _prune_empty_anchors(root: _Node) -> int:
    """Iteratively remove ``<a>`` elements that have no visible text in any
    descendant (table cells included) or wrap a nested ``<a>``, unwrapping
    any structural children and returning how many removed links to count."""
    order: list[_Node] = []
    visit_stack: list[_Node] = [root]
    while visit_stack:
        cur = visit_stack.pop()
        order.append(cur)
        for ch in cur.children:
            if isinstance(ch, _Node):
                visit_stack.append(ch)

    has_vis: dict[int, bool] = {}
    removed_anchor_links = 0
    for node in reversed(order):
        node_vis = False
        rebuilt: list[_Node | str] = []
        has_unwrapped_a = False
        for ch in node.children:
            if isinstance(ch, str):
                if not node_vis and any(_is_visible_char(c) for c in ch):
                    node_vis = True
                rebuilt.append(ch)
            else:
                ch_vis = has_vis.get(id(ch), False)
                if ch_vis:
                    node_vis = True
                if ch.tag == "a" and (not ch_vis or ch.unwrap_anchor):
                    has_unwrapped_a = True
                    if not ch.had_removed_link:
                        removed_anchor_links += 1
                    rebuilt.extend(ch.children)
                else:
                    rebuilt.append(ch)
        if has_unwrapped_a:
            node.children = rebuilt
        has_vis[id(node)] = node_vis
    return removed_anchor_links


def _escape_attr(val: str) -> str:
    """Escape only ``&``, ``<``, ``>`` and ``"`` inside double-quoted attributes;
    single quotes stay as written."""
    return (
        val.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _serialise_tree(
    root: _Node, body_attrs: list[tuple[str, str]]
) -> tuple[str, str, str]:
    body_attr_str = "".join(
        f' {name}="{_escape_attr(val)}"' for name, val in body_attrs
    )
    out_html: list[str] = [f"<html><body{body_attr_str}>"]
    out_text: list[str] = []
    text_buf: list[str] = []

    def flush_text_buf() -> None:
        if not text_buf:
            return
        combined = _cap_combining_marks("".join(text_buf))
        text_buf.clear()
        out_html.append(html.escape(combined, quote=False))
        out_text.append(combined)

    stack: list[tuple[_Node | str, bool]] = [
        (child, False) for child in reversed(root.children)
    ]

    while stack:
        item, closing = stack.pop()
        if isinstance(item, str):
            text_buf.append(item)
            continue
        flush_text_buf()
        if closing:
            out_html.append(f"</{item.tag}>")
            if item.tag in _TEXT_BREAK_TAGS:
                out_text.append("\n")
            elif item.tag in ("td", "th"):
                out_text.append(" ")
            continue

        attr_str = "".join(
            f' {name}="{_escape_attr(val)}"' for name, val in item.attrs
        )
        out_html.append(f"<{item.tag}{attr_str}>")
        if item.tag == "br" or item.tag in _TEXT_BREAK_TAGS:
            out_text.append("\n")
        if item.tag in VOID_ELEMENTS:
            continue
        stack.append((item, True))
        for child in reversed(item.children):
            stack.append((child, False))

    flush_text_buf()
    out_html.append("</body></html>")
    raw_text = "".join(out_text)
    lines = [" ".join(ln.split()) for ln in raw_text.split("\n")]
    compact_lines: list[str] = []
    for ln in lines:
        if ln:
            compact_lines.append(ln)
        elif compact_lines and compact_lines[-1] != "":
            compact_lines.append("")
    while compact_lines and compact_lines[-1] == "":
        compact_lines.pop()
    return "".join(out_html), raw_text, "\n".join(compact_lines)


def _sanitise_pass(raw: str) -> tuple[str, str, str, set[str], int, int]:
    builder = _SanitisingBuilder()
    for tok in _scan_tokens(raw, drop_cdata=True):
        kind = tok[0]
        if kind == "text":
            builder.on_text(tok[1])
        elif kind == "start":
            builder.on_start(tok[1], tok[2], tok[3])
        else:
            builder.on_end(tok[1])

    builder.removed_links += _prune_empty_anchors(builder.root)
    merged_body: dict[str, str] = dict(builder.html_attrs)
    merged_body.update(builder.body_attrs)
    rebuilt_html, raw_text, readable_text = _serialise_tree(
        builder.root, list(merged_body.items())
    )
    return (
        rebuilt_html,
        raw_text,
        readable_text,
        builder.removed_elements,
        builder.removed_attributes,
        builder.removed_links,
    )


def sanitise_html(raw: str) -> SanitiseResult:
    """Parse, allow-list filter, and rebuild an HTML draft body to a fixed point."""
    if not isinstance(raw, str):
        raise HTMLValidationError("HTML body must be a string")
    if len(raw) > MAX_HTML_CHARS:
        raise HTMLValidationError(
            f"HTML body exceeds {MAX_HTML_CHARS} characters (got {len(raw)})"
        )

    try:
        (
            rebuilt_html,
            raw_text,
            readable_text,
            removed_elements,
            removed_attributes,
            removed_links,
        ) = _sanitise_pass(raw)
        if not any(_is_visible_char(ch) for ch in raw_text):
            raise HTMLValidationError("HTML body has no visible text after sanitising")

        second_html, second_raw_text, second_text, _, _, _ = _sanitise_pass(
            rebuilt_html
        )
        if second_html != rebuilt_html:
            third_html, _, _, _, _, _ = _sanitise_pass(second_html)
            if third_html != second_html:
                raise HTMLValidationError(
                    "HTML body did not stabilise to a fixed point after sanitising"
                )
            rebuilt_html = second_html
            raw_text = second_raw_text
            readable_text = second_text
    except HTMLValidationError:
        raise
    except Exception as exc:
        raise HTMLValidationError(f"invalid HTML body ({type(exc).__name__}: {exc})") from None

    if not any(_is_visible_char(ch) for ch in raw_text):
        raise HTMLValidationError("HTML body has no visible text after sanitising")

    sanitised: dict[str, Any] | None = None
    if removed_elements or removed_attributes or removed_links:
        sanitised = {
            "removed_elements": sorted(removed_elements),
            "removed_attributes": removed_attributes,
            "removed_links": removed_links,
        }
    return SanitiseResult(html=rebuilt_html, text=readable_text, sanitised=sanitised)


# --- Stored-draft link & host inspection (fail-closed) ------------------------


@dataclass
class _ParsedTarget:
    scheme: str
    raw_host: str
    ascii_host: str
    port: int | None
    mailto_addrs: list[str]
    mailto_query: str
    has_userinfo: bool
    malformed: bool


def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _truncate_overlong_host(host: str) -> str:
    """Cut a > 253-char host to 80 chars preserving both prefix and registrable tail."""
    if len(host) <= TRUNCATED_HOST_CHARS:
        return host
    head = (TRUNCATED_HOST_CHARS - 1) // 2
    tail = TRUNCATED_HOST_CHARS - 1 - head
    return f"{host[:head]}…{host[-tail:]}"


def _is_numeric_host(host: str) -> bool:
    """True if ``host`` is an integer in any base, dotted numbers, or bracketed IP."""
    h = host.strip().rstrip(".").lower()
    if not h:
        return False
    if h.startswith("[") and h.endswith("]"):
        try:
            ipaddress.ip_address(h[1:-1])
            return True
        except ValueError:
            return False
    parts = h.split(".")
    return bool(parts) and all(bool(p) and _NUMERIC_LABEL_RE.match(p) is not None for p in parts)


def _to_ascii_and_display_host(raw_host: str) -> tuple[str, bool]:
    """Return ``(ascii_host, is_idn_or_punycode)`` without ever raising or
    passing hosts > 253 chars (or labels > 63 chars) into IDNA/punycode."""
    host = raw_host.strip().rstrip(".").lower()
    if not host:
        return "", False
    if len(host) > MAX_HOST_CHARS:
        return _truncate_overlong_host(host), False
    if host.startswith("[") and host.endswith("]"):
        return host, False
    has_non_ascii = not host.isascii()
    has_punycode = any(lb.startswith("xn--") for lb in host.split("."))
    if not has_non_ascii and not has_punycode:
        return host, False
    out_labels: list[str] = []
    for lb in host.split("."):
        if not lb:
            out_labels.append("")
            continue
        if len(lb) > 63:
            out_labels.append(lb)
            continue
        try:
            out_labels.append(lb.encode("idna").decode("ascii").lower())
        except UnicodeError:
            out_labels.append(lb)
    return ".".join(out_labels), True


def _decode_punycode_host(ascii_host: str) -> str:
    if len(ascii_host) > MAX_HOST_CHARS:
        return _truncate_overlong_host(ascii_host)
    labels: list[str] = []
    for lb in ascii_host.split("."):
        if lb.startswith("xn--") and len(lb) <= 63:
            try:
                labels.append(lb.encode("ascii").decode("idna"))
                continue
            except UnicodeError:
                pass
        labels.append(lb)
    return ".".join(labels)


def _strip_authority_to_host_and_port(
    authority: str,
) -> tuple[str, int | None, bool, bool]:
    """Return ``(raw_host_lower, port_or_None, has_userinfo, malformed)``."""
    has_userinfo = "@" in authority
    hostport = authority.rsplit("@", 1)[-1]
    if not hostport:
        return "", None, has_userinfo, True

    malformed = False
    port: int | None = None
    if hostport.startswith("["):
        close_b = hostport.find("]")
        if close_b == -1:
            return hostport.lower(), None, has_userinfo, True
        raw_host = hostport[: close_b + 1].lower()
        tail = hostport[close_b + 1 :]
        if tail.startswith(":"):
            if tail[1:].isdigit():
                port = int(tail[1:])
            elif tail[1:]:
                malformed = True
        elif tail:
            malformed = True
        try:
            ipaddress.ip_address(raw_host[1:-1])
        except ValueError:
            malformed = True
        return raw_host, port, has_userinfo, malformed

    if ":" in hostport:
        h_part, p_part = hostport.rsplit(":", 1)
        raw_host = h_part.lower().rstrip(".")
        if p_part.isdigit():
            port = int(p_part)
        elif p_part:
            malformed = True
    else:
        raw_host = hostport.lower().rstrip(".")

    if not raw_host or any(ch.isspace() or ch in "[]" for ch in raw_host):
        malformed = True
    if any(ch in "\uff0f\u2044\u2215" for ch in authority):
        malformed = True
    return raw_host, port, has_userinfo, malformed


def _safe_parse_target(raw_url: str, base_url: str = "") -> _ParsedTarget:
    """Parse any stored URL attribute without raising on malformed syntax."""
    u = "".join(
        ch for ch in raw_url.strip()
        if ord(ch) >= 0x20 and ch not in "\t\n\r"
    )
    if (
        base_url
        and u
        and not _SCHEME_RE.match(u)
        and not u.startswith(("//", "\\\\", "/\\", "\\/"))
    ):
        try:
            u = urljoin(base_url, u)
        except ValueError:
            pass

    m = _SCHEME_RE.match(u)
    scheme = m.group(0)[:-1].lower() if m else ""

    if scheme == "mailto":
        rest = u[m.end():] if m else ""
        path_part, _, query_part = rest.partition("?")
        path_part = path_part.split("#", 1)[0]
        raw_addrs = unquote(path_part)
        addrs: list[str] = []
        for piece in re.split(r"[,;\r\n]+", raw_addrs):
            cleaned_piece = piece.strip().lower()
            if not cleaned_piece:
                continue
            if ":" in cleaned_piece and "@" in cleaned_piece:
                cleaned_piece = cleaned_piece.rsplit(":", 1)[-1].strip()
            if cleaned_piece:
                addrs.append(cleaned_piece)
        first_raw_host = ""
        if addrs and "@" in addrs[0]:
            first_raw_host = addrs[0].rsplit("@", 1)[-1].rstrip(".")
        ascii_host, _ = _to_ascii_and_display_host(first_raw_host)
        malformed = (
            not addrs
            or any("@" not in a for a in addrs)
            or "\r" in raw_addrs
            or "\n" in raw_addrs
        )
        return _ParsedTarget(
            scheme="mailto",
            raw_host=first_raw_host.lower(),
            ascii_host=ascii_host,
            port=None,
            mailto_addrs=addrs,
            mailto_query=query_part,
            has_userinfo=False,
            malformed=malformed,
        )

    after_scheme = u[m.end():] if m else u
    if (
        scheme in ("http", "https", "ftp", "ws", "wss")
        or u.startswith(("//", "\\\\", "/\\", "\\/"))
        or (m is not None and after_scheme.startswith(("//", "\\\\", "/\\", "\\/")))
    ):
        slashed = after_scheme.lstrip("/\\")
        had_slashes = len(slashed) < len(after_scheme)
        end = len(slashed)
        for idx, ch in enumerate(slashed):
            if ch in "/\\?#":
                end = idx
                break
        authority = slashed[:end]
        raw_host, port, has_userinfo, malformed = _strip_authority_to_host_and_port(
            authority
        )
        if not had_slashes:
            malformed = True
        if not u.isascii() and len(raw_host) <= MAX_HOST_CHARS:
            try:
                sp = urlsplit(u if scheme else "https:" + u)
                _ = sp.port
            except ValueError:
                malformed = True

        ascii_host, _ = _to_ascii_and_display_host(raw_host)
        return _ParsedTarget(
            scheme=scheme or "http",
            raw_host=raw_host,
            ascii_host=ascii_host,
            port=port,
            mailto_addrs=[],
            mailto_query="",
            has_userinfo=has_userinfo,
            malformed=malformed,
        )

    return _ParsedTarget(
        scheme=scheme,
        raw_host="",
        ascii_host="",
        port=None,
        mailto_addrs=[],
        mailto_query="",
        has_userinfo=False,
        malformed=not bool(scheme),
    )


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _normalise_visible_text(text: str) -> str:
    """Normalise visible link/title text in 64 KiB stream-safe slices so NFKC
    cannot go quadratic or hold the GIL for > ~0.2 s."""
    if not text:
        return ""
    if text.isascii():
        return text.translate(_ASCII_CTRL_STRIP_MAP).lower()

    safe_text = _cap_combining_marks(text)
    n = len(safe_text)
    out_parts: list[str] = []
    pos = 0
    while pos < n:
        end = min(n, pos + _CHUNK_SIZE)
        if end < n:
            # Break on whitespace where possible so words stay intact across chunks.
            ws_pos = safe_text.rfind(" ", pos, end)
            if ws_pos > pos + (_CHUNK_SIZE // 2):
                end = ws_pos + 1
        chunk = safe_text[pos:end]
        pos = end
        if chunk.isascii():
            out_parts.append(chunk.translate(_ASCII_CTRL_STRIP_MAP).lower())
            continue
        stripped = _IGNORABLE_SPAN_RE.sub("", chunk)
        folded_seps = stripped.translate(_LOOKALIKE_SEP_MAP)
        nfkc = unicodedata.normalize("NFKC", folded_seps)
        cleaned = _IGNORABLE_SPAN_RE.sub("", nfkc)
        out_parts.append(cleaned.casefold())
    return "".join(out_parts)


def _find_mixed_ascii_nonascii_token(text: str) -> tuple[str, list[str]] | None:
    """If ``text`` contains a whitespace-free token mixing ASCII letters with
    any non-ASCII character (after removing default-ignorable/format marks),
    return ``(token, [codepoint_labels])``; pure non-Latin text returns ``None``."""
    if not text or text.isascii():
        return None
    for raw_tok in text.split():
        tok = _IGNORABLE_SPAN_RE.sub("", raw_tok)
        if not tok or tok.isascii():
            continue
        has_ascii_alpha = False
        unusual: list[str] = []
        seen_cp: set[int] = set()
        for ch in tok:
            code = ord(ch)
            if (0x41 <= code <= 0x5A) or (0x61 <= code <= 0x7A):
                has_ascii_alpha = True
            elif code > 0x7F and code not in seen_cp:
                seen_cp.add(code)
                if len(unusual) < 5:
                    unusual.append(f"U+{code:04X}")
        if has_ascii_alpha and unusual:
            return raw_tok, unusual
    return None


def _looks_like_hostname(candidate: str, *, has_path_or_www: bool = False) -> bool:
    host = candidate.strip(".")
    if not host or len(host) > MAX_HOST_CHARS:
        return False
    if _IPV4_RE.match(host):
        return all(0 <= int(part) <= 255 for part in host.split("."))
    labels = host.split(".")
    if len(labels) < 2:
        return False
    for lb in labels:
        if not lb or len(lb) > 63 or lb.startswith("-") or lb.endswith("-"):
            return False
        if not all(ch.isalnum() or ch == "-" for ch in lb):
            return False
    tld = labels[-1]
    tld_ok = (len(tld) >= 2 and tld.isalpha()) or (
        tld.startswith("xn--") and len(tld) >= 5
    )
    if not tld_ok:
        return False
    if len(labels) == 2 and not has_path_or_www and tld in _NON_TLD_EXTS:
        return False
    return True


def _iter_spanned_email_and_host_tokens(
    norm_text: str,
    host_eval_cache: dict[tuple[str, bool], tuple[str, str] | None] | None = None,
    *,
    dedupe: bool = False,
) -> list[tuple[int, int, str, Any]]:
    """Linear-time scan extracting ``(start, end, kind, payload)`` spans for
    email and hostname candidates anywhere in ``norm_text``."""
    spans: list[tuple[int, int, str, Any]] = []
    cache = host_eval_cache if host_eval_cache is not None else {}
    seen_tokens: set[tuple[str, bool]] | None = set() if dedupe else None
    n = len(norm_text)

    def eval_host(cand: str, has_ctx: bool) -> tuple[str, str] | None:
        ck = (cand, has_ctx)
        if ck in cache:
            return cache[ck]
        if _looks_like_hostname(cand, has_path_or_www=has_ctx):
            ascii_h, _ = _to_ascii_and_display_host(cand)
            res: tuple[str, str] | None = (cand, ascii_h)
        else:
            res = None
        cache[ck] = res
        return res

    for tok_m in _CANDIDATE_TOKEN_RE.finditer(norm_text):
        t_start, i = tok_m.span()
        t_end = i
        while t_start < t_end and norm_text[t_start] in "._+-:":
            t_start += 1
        while t_end > t_start and norm_text[t_end - 1] in "._+-:":
            t_end -= 1
        if t_end <= t_start or (t_end - t_start) > 320:
            continue
        token = norm_text[t_start:t_end]

        has_slash_after = i < n and norm_text[i] in "/\\?"
        if seen_tokens is not None:
            tk = (token, has_slash_after)
            if tk in seen_tokens:
                continue
            seen_tokens.add(tk)

        if "@" in token:
            local, _, dom_port = token.partition("@")
            dom = dom_port.split(":", 1)[0].strip(".")
            if local and "@" not in dom and eval_host(dom, True) is not None:
                spans.append((t_start, t_end, "email", (f"{local}@{dom}", dom)))
            continue

        if ":" in token:
            h_only, _, p_only = token.partition(":")
            if p_only.isdigit():
                host_clean = h_only.strip("._+-")
                has_ctx = has_slash_after or host_clean.startswith("www.")
                ev = eval_host(host_clean, has_ctx)
                if ev is not None:
                    spans.append((t_start, t_end, "host", ev))
                continue
            sub_pos = t_start
            for sub in token.split(":"):
                sub_s = sub_pos
                sub_e = sub_pos + len(sub)
                sub_pos = sub_e + 1
                while sub_s < sub_e and norm_text[sub_s] in "._+-":
                    sub_s += 1
                while sub_e > sub_s and norm_text[sub_e - 1] in "._+-":
                    sub_e -= 1
                sub_clean = norm_text[sub_s:sub_e]
                ev = eval_host(sub_clean, has_slash_after)
                if ev is not None:
                    spans.append((sub_s, sub_e, "host", ev))
            continue

        host_clean = token.strip("._+-")
        has_ctx = has_slash_after or host_clean.startswith("www.")
        ev = eval_host(host_clean, has_ctx)
        if ev is not None:
            spans.append((t_start, t_end, "host", ev))

    return spans


def _collapse_ws_around_dots(text: str) -> str:
    """Remove all whitespace immediately adjacent to ``.`` in linear time."""
    if "." not in text:
        return text
    parts = text.split(".")
    last = len(parts) - 1
    if last == 1:
        return parts[0].rstrip() + "." + parts[1].lstrip()
    out: list[str] = [parts[0].rstrip()]
    for seg in parts[1:last]:
        out.append(seg.strip())
    out.append(parts[last].lstrip())
    return ".".join(out)


def _extract_spanned_claims(
    norm: str,
    *,
    url_cache: dict[str, _ParsedTarget] | None = None,
    host_eval_cache: dict[tuple[str, bool], tuple[str, str] | None] | None = None,
    dedupe: bool = False,
) -> list[tuple[int, int, str, Any]]:
    """Single linear pass extracting ``(start, end, kind, payload)`` URL, email,
    and hostname claims after removing whitespace of any kind around dots."""
    if not norm or not norm.strip():
        return []
    if "." not in norm and "@" not in norm and ":" not in norm:
        return []

    collapsed = _collapse_ws_around_dots(norm) if "." in norm else norm
    ucache = url_cache if url_cache is not None else {}

    url_spans: list[tuple[int, int, str, Any]] = []
    seen_urls: set[str] | None = set() if dedupe else None
    has_url_match = ":" in collapsed and _EXPLICIT_URL_TOKEN_RE.search(collapsed) is not None
    if has_url_match:
        remaining_chars = list(collapsed)
        for m in _EXPLICIT_URL_TOKEN_RE.finditer(collapsed):
            s_idx, e_idx = m.span()
            raw_tok = m.group(0).rstrip(".,;:!?)]}'\"»”›*")
            tok_end = max(s_idx + 1, s_idx + len(raw_tok))
            remaining_chars[s_idx:e_idx] = [" "] * (e_idx - s_idx)
            if seen_urls is not None:
                if raw_tok in seen_urls:
                    continue
                seen_urls.add(raw_tok)
            parsed = ucache.get(raw_tok)
            if parsed is None:
                parsed = _safe_parse_target(raw_tok)
                ucache[raw_tok] = parsed
            url_spans.append((s_idx, tok_end, "url", (raw_tok, parsed)))
        rest_str = "".join(remaining_chars)
    else:
        rest_str = collapsed

    eh_spans = _iter_spanned_email_and_host_tokens(
        rest_str, host_eval_cache, dedupe=dedupe
    )
    if not url_spans:
        return eh_spans
    if not eh_spans:
        return url_spans

    if dedupe:
        return url_spans + eh_spans

    merged: list[tuple[int, int, str, Any]] = []
    iu = 0
    ie = 0
    nu = len(url_spans)
    ne = len(eh_spans)
    while iu < nu and ie < ne:
        if url_spans[iu][0] <= eh_spans[ie][0]:
            merged.append(url_spans[iu])
            iu += 1
        else:
            merged.append(eh_spans[ie])
            ie += 1
    if iu < nu:
        merged.extend(url_spans[iu:])
    if ie < ne:
        merged.extend(eh_spans[ie:])
    return merged


def _boundary_slice_for_split_anchor(norm_part: str) -> str:
    """Keep only the leading and trailing whitespace-delimited boundary tokens of
    an anchor's normalised text so adjacent split-anchor checking stays O(anchors)."""
    if len(norm_part) <= 650:
        return norm_part
    first_ws = -1
    for idx, ch in enumerate(norm_part):
        if ch.isspace():
            first_ws = idx
            break
    if first_ws == -1:
        return norm_part[:330] + norm_part[-320:]
    last_ws = -1
    for idx in range(len(norm_part) - 1, first_ws, -1):
        if norm_part[idx].isspace():
            last_ws = idx
            break
    if last_ws == -1 or last_ws <= first_ws:
        head = norm_part[: min(first_ws, 330)]
        tail = norm_part[first_ws + 1 :]
        if len(tail) > 330:
            tail = tail[-330:]
        return f"{head} {tail}"
    head = norm_part[: min(first_ws, 330)]
    tail = norm_part[last_ws + 1 :]
    if len(tail) > 330:
        tail = tail[-330:]
    return f"{head} {tail}"


def _check_claim_against_target(
    kind: str,
    payload: Any,
    label_text: str,
    href: str,
    target: _ParsedTarget,
) -> str | None:
    if kind == "url":
        raw_tok, claimed = payload
        if claimed.has_userinfo:
            return (
                f"link mismatch: text {_short(label_text)!r} contains embedded "
                f"userinfo while pointing to {_short(href)!r}"
            )
        if claimed.scheme == "mailto":
            if target.scheme != "mailto" or claimed.mailto_addrs != target.mailto_addrs:
                return (
                    f"link mismatch: text shows {_short(raw_tok)!r} "
                    f"but href points to {_short(href)!r}"
                )
            return None
        if target.scheme == "mailto":
            return (
                f"link mismatch: text shows web URL {_short(raw_tok)!r} "
                f"but href is {_short(href)!r}"
            )
        if claimed.raw_host and (
            claimed.raw_host != target.raw_host
            or claimed.ascii_host != target.ascii_host
        ):
            return (
                f"link mismatch: text shows host {_short(claimed.raw_host, 80)!r} "
                f"but href points to {_short(target.raw_host or href, 80)!r}"
            )
        if claimed.scheme == "https" and target.scheme == "http":
            return (
                f"link mismatch: text shows https URL {_short(raw_tok)!r} "
                f"but href uses http ({_short(href)!r})"
            )
        c_port = _effective_port(claimed.scheme, claimed.port)
        t_port = _effective_port(target.scheme, target.port)
        if c_port is not None and t_port is not None and c_port != t_port:
            return (
                f"link mismatch: text URL port {c_port} differs from "
                f"target {_short(href)!r}"
            )
        return None

    if kind == "email":
        claimed_email, claimed_dom = payload
        if target.scheme != "mailto":
            return (
                f"link mismatch: text shows address {claimed_email!r} "
                f"but href points to {_short(href)!r}"
            )
        if target.mailto_addrs != [claimed_email]:
            return (
                f"link mismatch: text shows address {claimed_email!r} "
                f"but mailto target is {_short(href)!r}"
            )
        if claimed_dom != target.raw_host:
            return (
                f"link mismatch: text shows domain {claimed_dom!r} "
                f"but href points to {_short(target.raw_host or href, 80)!r}"
            )
        return None

    claimed_host, claimed_ascii = payload
    if claimed_host != target.raw_host or claimed_ascii != target.ascii_host:
        return (
            f"link mismatch: text shows host {_short(claimed_host, 80)!r} "
            f"but href points to {_short(target.raw_host or href, 80)!r}"
        )
    return None


def _check_claims_against_target(
    claims: list[tuple[int, int, str, Any]],
    label_text: str,
    href: str,
    target: _ParsedTarget,
) -> str | None:
    for _, _, kind, payload in claims:
        msg = _check_claim_against_target(kind, payload, label_text, href, target)
        if msg is not None:
            return msg
    return None


def _target_intrinsic_warnings(href: str, target: _ParsedTarget) -> list[tuple[str, str]]:
    """Return ``[(kind, warning_message)]`` for intrinsic properties of ``target``."""
    out: list[tuple[str, str]] = []
    trimmed = href.strip()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in trimmed):
        out.append(("malformed_url", f"control or tab character inside link target: {_short(href)!r}"))
    if target.malformed:
        out.append(("malformed_url", f"unparseable or malformed link target: {_short(href)!r}"))
    if target.scheme and target.scheme not in ("http", "https", "mailto", "cid"):
        out.append((
            "nonstandard_scheme",
            f"non-standard link scheme {target.scheme!r} in {_short(href)!r}",
        ))
    if target.has_userinfo:
        out.append((
            "userinfo",
            f"link target {_short(href)!r} contains embedded userinfo "
            f"before host {_short(target.raw_host, 80)!r}",
        ))
    if target.raw_host:
        if len(target.raw_host) > MAX_HOST_CHARS:
            cut_h = _truncate_overlong_host(target.raw_host)
            out.append((
                "overlong_host",
                f"overlong link host ({len(target.raw_host)} chars, truncated): {cut_h}",
            ))
        else:
            if _is_numeric_host(target.raw_host):
                out.append(("numeric_host", f"numeric link host address: {target.raw_host}"))
            if any(ch in _SUSPICIOUS_HOST_CHARS for ch in target.raw_host):
                out.append((
                    "suspicious_host",
                    f"suspicious encoded or look-alike host {target.raw_host!r} "
                    f"in {_short(href)!r}",
                ))
            ascii_h, is_idn = _to_ascii_and_display_host(target.raw_host)
            if is_idn:
                shown = (
                    _decode_punycode_host(ascii_h)
                    if target.raw_host == ascii_h
                    else target.raw_host
                )
                out.append((
                    "idn_host",
                    f"non-ASCII or punycode link host: {ascii_h} (written as {shown})",
                ))
    if target.scheme == "mailto":
        if len(target.mailto_addrs) > 1:
            out.append((
                "mailto_multi",
                f"mailto link specifies multiple recipients: {_short(href)!r}",
            ))
        if target.mailto_query:
            out.append((
                "mailto_query",
                f"mailto link carries query parameters: {_short(href)!r}",
            ))
    return out


# --- Promise 2: parser-independent raw text host scan -------------------------


def _clean_scanned_host_candidate(raw_candidate: str) -> str:
    decoded = unquote(raw_candidate).translate(_TAB_CR_LF_DELETE).strip().lower()
    if not decoded:
        return ""
    # Strip any path/query/fragment separator revealed by percent-decoding.
    for idx, ch in enumerate(decoded):
        if ch in "/\\?#":
            decoded = decoded[:idx]
            break
    decoded = decoded.rstrip(".")
    if not decoded:
        return ""
    if decoded.startswith("["):
        close_b = decoded.find("]")
        if close_b != -1:
            return decoded[: close_b + 1]
        return ""
    if ":" in decoded:
        decoded = decoded.split(":", 1)[0].rstrip(".")
    if not decoded or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() or ch in "<>\"'`()[]{}"
        for ch in decoded
    ):
        return ""
    return decoded


def _extract_host_from_scan_authority(authority: str) -> str:
    hostport = authority.rsplit("@", 1)[-1]
    if not hostport:
        return ""
    if hostport.startswith("["):
        close_b = hostport.find("]")
        if close_b == -1:
            return ""
        tail = hostport[close_b + 1 :]
        if tail and not tail.startswith(":"):
            return ""
        return _clean_scanned_host_candidate(hostport[: close_b + 1])
    if ":" in hostport:
        hostport = hostport.split(":", 1)[0]
    return _clean_scanned_host_candidate(hostport)


def _looks_like_slashless_scan_host(host: str, scheme_low: str = "") -> bool:
    if "." in host:
        return True
    if host.startswith("[") and host.endswith("]"):
        return True
    if not _is_numeric_host(host):
        return False
    return not (scheme_low == "ws" and len(host) <= 3 and host.isdigit())


def _scan_variant_hosts(variant: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m_url in _SCAN_URL_RE.finditer(variant):
        scheme_raw, scheme_slashes, rel_slashes, auth = m_url.groups()
        h = _extract_host_from_scan_authority(auth)
        if not h:
            continue
        if scheme_raw is not None:
            if not scheme_slashes and not _looks_like_slashless_scan_host(
                h, scheme_raw.lower()
            ):
                continue
        elif rel_slashes is not None:
            pos = m_url.start()
            has_delim_before = (
                pos == 0
                or variant[pos - 1].isspace()
                or variant[pos - 1] in "=\"'`(;,"
            )
            if not has_delim_before and not _looks_like_slashless_scan_host(h):
                continue
        counts[h] = counts.get(h, 0) + 1

    if "@" in variant:
        for m_ui in _SCAN_USERINFO_URL_RE.finditer(variant):
            raw_span = m_ui.group(1)
            if "@" not in raw_span:
                continue
            before_at, _, after_at = raw_span.rpartition("@")
            if not any(ch in before_at for ch in "\"'`<>)"):
                continue
            for idx, ch in enumerate(after_at):
                if ch in "\"'`<>)":
                    after_at = after_at[:idx]
                    break
            uh = _extract_host_from_scan_authority(after_at)
            if uh:
                counts[uh] = counts.get(uh, 0) + 1

    for m_mail in _SCAN_MAILTO_RE.finditer(variant):
        rest = m_mail.group(1).split("#", 1)[0]
        addr_part, _, query_part = rest.partition("?")
        for piece in _MAILTO_ADDR_SPLIT_RE.split(unquote(addr_part)):
            if "@" in piece:
                mh = _clean_scanned_host_candidate(piece.rsplit("@", 1)[-1])
                if mh:
                    counts[mh] = counts.get(mh, 0) + 1
        if query_part:
            for qpiece in _MAILTO_QUERY_SPLIT_RE.split(unquote(query_part)):
                if "@" in qpiece:
                    qh = _clean_scanned_host_candidate(qpiece.rsplit("@", 1)[-1])
                    if qh:
                        counts[qh] = counts.get(qh, 0) + 1
    return counts


def _scan_raw_text_hosts(raw_text: str) -> list[tuple[str, int]]:
    """Scan raw text (no quote, tag, attribute, or comment state) across bounded
    slices under the union over readings of character references (none decoded,
    browser character-reference rules, and 3-pass fixed point) and whitespace
    (tab/CR/LF deleted and replaced by a space)."""
    totals: dict[str, int] = {}
    n = len(raw_text)
    pos = 0
    max_slice = MAX_INSPECT_HTML_CHARS

    while pos < n:
        end = min(n, pos + _CHUNK_SIZE)
        if end < n:
            # Break only on ASCII space or a closing tag '</' (whose '/' already
            # terminates any URL authority) so no URL up to 2,000,000 chars is cut.
            while (
                end < n
                and end - pos < max_slice
                and raw_text[end] != " "
                and raw_text[end : end + 2] != "</"
            ):
                end += 1
        chunk = raw_text[pos:end]
        pos = end

        if "&" in chunk:
            browser_once = html.unescape(chunk)
            browser_nested = _unescape_fully(browser_once, rounds=2)
            all_three = _decode_scan_entities(chunk, max_passes=3)
            charref_readings: tuple[str, ...] = (
                chunk,
                browser_once,
                browser_nested,
                all_three,
            )
        else:
            charref_readings = (chunk,)

        seen_variants: set[str] = set()
        chunk_counts: dict[str, int] = {}
        for cread in charref_readings:
            has_ctrl_ws = (
                "\n" in cread or "\r" in cread or "\t" in cread or "\x00" in cread
            )
            ws_readings = (
                (
                    cread.translate(_TAB_CR_LF_DELETE),
                    cread.translate(_TAB_CR_LF_SPACE),
                )
                if has_ctrl_ws
                else (cread,)
            )
            for variant in ws_readings:
                if variant in seen_variants:
                    continue
                seen_variants.add(variant)
                v_counts = _scan_variant_hosts(variant)
                for h, cnt in v_counts.items():
                    if cnt > chunk_counts.get(h, 0):
                        chunk_counts[h] = cnt

        for h, cnt in chunk_counts.items():
            totals[h] = totals.get(h, 0) + cnt

    return list(totals.items())


def inspect_draft_links(
    raw_html: str,
    limit: int = MAX_PREVIEW_LINKS,
    *,
    max_text_chars: int | None = None,
) -> dict[str, Any]:
    """Fail-closed, linear-time link and destination-host inspector over stored HTML."""
    if not isinstance(raw_html, str) or not raw_html:
        return {"links": [], "link_hosts": [], "warnings": []}

    text_cap = (
        max_text_chars
        if max_text_chars is not None
        else (MAX_PREVIEW_LINK_TEXT_CHARS if limit <= MAX_PREVIEW_LINKS else None)
    )

    unverified = False
    tier1_unverified_size: list[str] = []
    tier2_mismatch: list[str] = []
    tier3_outside_markup: list[str] = []
    tier4_omission: list[str] = []
    tier5_by_kind: dict[str, list[str]] = {}
    seen_warnings: set[str] = set()

    def add_tier1(msg: str) -> None:
        if msg not in seen_warnings:
            seen_warnings.add(msg)
            tier1_unverified_size.append(msg)

    def add_mismatch(msg: str) -> None:
        if msg not in seen_warnings:
            seen_warnings.add(msg)
            tier2_mismatch.append(msg)

    def add_tier5(kind: str, msg: str) -> None:
        if msg not in seen_warnings:
            seen_warnings.add(msg)
            tier5_by_kind.setdefault(kind, []).append(msg)

    oversize = len(raw_html) > MAX_INSPECT_HTML_CHARS
    if oversize:
        unverified = True
        add_tier1(
            f"draft HTML exceeds {MAX_INSPECT_HTML_CHARS} characters "
            f"(got {len(raw_html)}); full link inspection truncated to raw host scan"
        )

    anchor_records: list[tuple[str, str, str, int]] = []  # (text, href, title, run_id)
    embedded_urls: list[tuple[str, str, str]] = []  # (tag, attr, url)
    base_href = ""

    if not oversize:
        try:
            current_href: str | None = None
            current_title: str = ""
            label_parts: list[str] = []
            table_depth = 0
            run_id = 0
            prev_ended_anchor = False

            def flush_anchor() -> None:
                nonlocal current_href, current_title, label_parts, prev_ended_anchor
                if current_href is None:
                    return
                full_label = " ".join("".join(label_parts).split())
                anchor_records.append((full_label, current_href, current_title, run_id))
                current_href = None
                current_title = ""
                label_parts = []
                prev_ended_anchor = True

            for tok in _scan_tokens(raw_html, drop_cdata=False):
                kind = tok[0]
                if kind == "text":
                    txt = tok[1]
                    if current_href is not None:
                        label_parts.append(txt)
                    elif txt:
                        prev_ended_anchor = False
                    continue

                if kind == "end":
                    etag = tok[1]
                    if etag == "table" and table_depth > 0:
                        table_depth -= 1
                    if etag == "a":
                        flush_anchor()
                    elif current_href is not None:
                        if etag in _TABLE_SUBELEMENTS and table_depth == 0:
                            pass
                        elif etag in ("td", "th") or etag in _TEXT_BREAK_TAGS:
                            label_parts.append(" ")
                    elif etag in _TEXT_BREAK_TAGS or etag == "br":
                        prev_ended_anchor = False
                    continue

                # kind == "start"
                stag, sattrs, self_closing = tok[1], tok[2], tok[3]
                if stag == "table" and not self_closing:
                    table_depth += 1

                first_by_name: dict[str, str] = {}
                for aname, aval in sattrs:
                    if aname not in first_by_name and isinstance(aval, str):
                        first_by_name[aname] = aval

                if stag == "base" and not base_href:
                    cand_base = first_by_name.get("href", "").strip()
                    if cand_base:
                        base_href = cand_base

                if stag == "a":
                    if current_href is not None:
                        flush_anchor()
                    if not prev_ended_anchor:
                        run_id += 1
                    prev_ended_anchor = False
                    ahref = (
                        first_by_name.get("href")
                        if "href" in first_by_name
                        else first_by_name.get("xlink:href")
                    )
                    if ahref is not None and ahref.strip():
                        current_href = ahref.strip()
                        current_title = first_by_name.get("title", "")
                        label_parts = []
                        if self_closing:
                            flush_anchor()
                    for uname in URL_INSPECT_ATTRS:
                        if uname in ("href", "xlink:href"):
                            continue
                        uval = first_by_name.get(uname, "").strip()
                        if not uval:
                            continue
                        if uname == "srcset":
                            for sc_part in uval.split(","):
                                sc_tokens = sc_part.strip().split()
                                if sc_tokens:
                                    embedded_urls.append((stag, uname, sc_tokens[0]))
                        else:
                            embedded_urls.append((stag, uname, uval))
                    continue

                # Non-anchor element: collect all URL-bearing attributes.
                for uname in URL_INSPECT_ATTRS:
                    uval = first_by_name.get(uname, "").strip()
                    if not uval:
                        continue
                    if uname == "srcset":
                        for sc_part in uval.split(","):
                            sc_tokens = sc_part.strip().split()
                            if sc_tokens:
                                embedded_urls.append((stag, uname, sc_tokens[0]))
                    else:
                        embedded_urls.append((stag, uname, uval))

                if current_href is not None:
                    if stag in _TABLE_SUBELEMENTS and table_depth == 0:
                        pass
                    elif stag == "br" or stag in _TEXT_BREAK_TAGS:
                        label_parts.append(" ")
                elif stag == "br" or stag in _TEXT_BREAK_TAGS:
                    prev_ended_anchor = False

            flush_anchor()
        except Exception as exc:
            unverified = True
            add_tier1(f"draft HTML link inspection failed ({type(exc).__name__})")

    # Aggregate hosts, distinct anchor links, and warnings in O(N).
    host_stats: dict[str, dict[str, Any]] = {}
    target_cache: dict[str, _ParsedTarget] = {}
    intrinsic_cache: dict[str, list[tuple[str, str]]] = {}
    norm_cache: dict[str, str] = {}
    claims_cache: dict[str, list[tuple[int, int, str, Any]]] = {}
    claim_url_cache: dict[str, _ParsedTarget] = {}
    claim_host_cache: dict[tuple[str, bool], tuple[str, str] | None] = {}

    def get_target(raw_u: str) -> _ParsedTarget:
        cached = target_cache.get(raw_u)
        if cached is None:
            cached = _safe_parse_target(raw_u, base_href)
            target_cache[raw_u] = cached
        return cached

    def get_intrinsic(raw_u: str, tgt: _ParsedTarget) -> list[tuple[str, str]]:
        cached = intrinsic_cache.get(raw_u)
        if cached is None:
            cached = _target_intrinsic_warnings(raw_u, tgt)
            intrinsic_cache[raw_u] = cached
        return cached

    def get_norm(txt: str) -> str:
        cached = norm_cache.get(txt)
        if cached is None:
            cached = _normalise_visible_text(txt)
            norm_cache[txt] = cached
        return cached

    def get_claims(txt: str) -> list[tuple[int, int, str, Any]]:
        cached = claims_cache.get(txt)
        if cached is None:
            cached = _extract_spanned_claims(
                get_norm(txt),
                url_cache=claim_url_cache,
                host_eval_cache=claim_host_cache,
                dedupe=True,
            )
            claims_cache[txt] = cached
        return cached

    def _build_host_entry(raw_h: str) -> tuple[str, str | None, bool, bool] | None:
        """Return ``(key, as_written, is_overlong, is_numeric)``."""
        raw_trimmed = raw_h.strip().lower()
        clean_raw = unquote(raw_trimmed).translate(_TAB_CR_LF_DELETE).rstrip(".")
        if not clean_raw:
            return None
        if len(clean_raw) > MAX_HOST_CHARS:
            cut_key = _truncate_overlong_host(clean_raw)
            return cut_key, None, True, False
        is_num = _is_numeric_host(clean_raw)
        ascii_h, is_idn = _to_ascii_and_display_host(clean_raw)
        key = ascii_h or clean_raw
        if not key:
            return None
        written: str | None = None
        if is_idn:
            dec = _decode_punycode_host(key) if clean_raw == key else clean_raw
            if dec != key:
                written = dec
        elif raw_trimmed != key and len(raw_trimmed) <= MAX_HOST_CHARS:
            written = raw_trimmed
        return key, written, False, is_num

    def _target_raw_hosts(target: _ParsedTarget) -> list[str]:
        if target.scheme == "mailto":
            out_h: list[str] = []
            seen_m: set[str] = set()
            for addr in target.mailto_addrs:
                if "@" in addr:
                    dom = addr.rsplit("@", 1)[-1].lower()
                    if dom and dom not in seen_m:
                        seen_m.add(dom)
                        out_h.append(dom)
            if target.mailto_query:
                for qpiece in re.split(r"[&,;=+\s]+", unquote(target.mailto_query).lower()):
                    if "@" in qpiece:
                        q_dom = qpiece.rsplit("@", 1)[-1].rstrip(".")
                        if q_dom and q_dom not in seen_m:
                            seen_m.add(q_dom)
                            out_h.append(q_dom)
            return out_h
        return [target.raw_host] if target.raw_host else []

    def record_single_host(
        raw_h: str, *, is_anchor: bool, is_mismatch: bool = False
    ) -> str | None:
        built = _build_host_entry(raw_h)
        if built is None:
            return None
        key, written, _, _ = built
        entry = host_stats.get(key)
        if entry is None:
            entry = {
                "host": key,
                "count": 0,
                "anchor_count": 0,
                "embedded_count": 0,
                "mismatch_count": 0,
                "unparsed": False,
                "as_written": written,
            }
            host_stats[key] = entry
        elif entry["as_written"] is None and written is not None:
            entry["as_written"] = written
        entry["count"] += 1
        if is_anchor:
            entry["anchor_count"] += 1
        else:
            entry["embedded_count"] += 1
        if is_mismatch:
            entry["mismatch_count"] += 1
        return key

    def record_host(
        target: _ParsedTarget, *, is_anchor: bool, is_mismatch: bool = False
    ) -> list[str]:
        keys: list[str] = []
        for rh in _target_raw_hosts(target):
            k = record_single_host(rh, is_anchor=is_anchor, is_mismatch=is_mismatch)
            if k is not None:
                keys.append(k)
        return keys

    distinct_pairs_order: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    mismatch_pairs: set[tuple[str, str]] = set()
    checked_pairs: set[tuple[str, str, str]] = set()
    pair_had_mismatch: dict[tuple[str, str, str], bool] = {}
    runs: dict[int, list[tuple[str, str, _ParsedTarget, tuple[str, str]]]] = {}

    for full_text, href, title, r_id in anchor_records:
        target = get_target(href)
        clipped_text = full_text[:text_cap] if text_cap is not None else full_text
        shown_href = _short(href, MAX_HREF_CHARS) if len(href) > MAX_HREF_CHARS else href
        pair_key = (clipped_text, shown_href)
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            distinct_pairs_order.append(pair_key)

        check_key = (full_text, href, title)
        if check_key not in checked_pairs:
            checked_pairs.add(check_key)
            link_mismatched = False
            for w_kind, w_msg in get_intrinsic(href, target):
                if w_kind in ("mailto_multi", "mailto_query"):
                    add_mismatch(w_msg)
                    link_mismatched = True
                else:
                    add_tier5(w_kind, w_msg)
            if any(ch in ("\u202d", "\u202e") for ch in full_text) or any(
                ch in ("\u202d", "\u202e") for ch in title
            ):
                add_tier5(
                    "bidi_override",
                    f"bidirectional override character in link text for {_short(href)!r}",
                )
            norm_ft = get_norm(full_text)
            norm_ti = get_norm(title)
            if not norm_ft.strip() and not norm_ti.strip():
                add_tier5("empty_text", f"link has no visible text: {_short(href)!r}")
            else:
                text_mismatched = False
                mismatch = _check_claims_against_target(
                    get_claims(full_text), full_text, href, target
                )
                if mismatch is not None:
                    add_mismatch(mismatch)
                    text_mismatched = True
                elif title:
                    title_mismatch = _check_claims_against_target(
                        get_claims(title), title, href, target
                    )
                    if title_mismatch is not None:
                        add_mismatch(f"link title mismatch ({title_mismatch})")
                        text_mismatched = True

                if not text_mismatched:
                    mixed_hit = _find_mixed_ascii_nonascii_token(full_text)
                    if mixed_hit is not None:
                        mixed_tok, cp_list = mixed_hit
                        add_mismatch(
                            f"link mismatch: link text {_short(mixed_tok, 60)!r} mixes "
                            f"ASCII letters with non-ASCII character(s) "
                            f"{', '.join(cp_list)} pointing to {_short(href)!r}"
                        )
                        text_mismatched = True
                if text_mismatched:
                    link_mismatched = True
            pair_had_mismatch[check_key] = link_mismatched
        else:
            link_mismatched = pair_had_mismatch[check_key]

        if link_mismatched:
            mismatch_pairs.add(pair_key)
        record_host(target, is_anchor=True, is_mismatch=link_mismatched)
        runs.setdefault(r_id, []).append((full_text, href, target, pair_key))

    # Linear-time adjacent split-anchor check over boundary slices only.
    for run_items in runs.values():
        if len(run_items) < 2:
            continue
        nonempty = [
            (_boundary_slice_for_split_anchor(get_norm(ft)), href, target, p_key)
            for ft, href, target, p_key in run_items
            if get_norm(ft)
        ]
        if len(nonempty) < 2:
            continue
        ends: list[int] = []
        acc = 0
        for norm_p, _, _, _ in nonempty:
            acc += len(norm_p)
            ends.append(acc)
        norm_combined = "".join(norm_p for norm_p, _, _, _ in nonempty)
        spans = _extract_spanned_claims(
            norm_combined,
            url_cache=claim_url_cache,
            host_eval_cache=claim_host_cache,
            dedupe=False,
        )
        cursor = 0
        n_anchors = len(ends)
        for s_idx, e_idx, kind, payload in spans:
            while cursor < n_anchors and ends[cursor] <= s_idx:
                cursor += 1
            if cursor >= n_anchors:
                break
            first_a = cursor
            last_a = first_a
            while last_a + 1 < n_anchors and ends[last_a] < e_idx:
                last_a += 1
            if first_a < last_a:
                span_label = norm_combined[s_idx:e_idx]
                seen_hrefs: set[str] = set()
                for a_idx in range(first_a, last_a + 1):
                    _, href_a, target_a, p_key_a = nonempty[a_idx]
                    if href_a in seen_hrefs:
                        continue
                    seen_hrefs.add(href_a)
                    split_warn = _check_claim_against_target(
                        kind, payload, span_label, href_a, target_a
                    )
                    if split_warn is not None:
                        add_mismatch(f"adjacent split link ({split_warn})")
                        mismatch_pairs.add(p_key_a)
                        for rh in _target_raw_hosts(target_a):
                            built = _build_host_entry(rh)
                            if built is not None and built[0] in host_stats:
                                host_stats[built[0]]["mismatch_count"] += 1

    for etag, eattr, eurl in embedded_urls:
        etarget = get_target(eurl)
        e_mismatch = False
        for w_kind, w_msg in get_intrinsic(eurl, etarget):
            if w_kind in ("mailto_multi", "mailto_query"):
                add_mismatch(w_msg)
                e_mismatch = True
            else:
                add_tier5(w_kind, w_msg)
        record_host(etarget, is_anchor=False, is_mismatch=e_mismatch)
        if eattr in ("href", "xlink:href", "action", "formaction"):
            add_tier5(
                "non_anchor_active",
                f"non-anchor active URL on <{etag} {eattr}>: {_short(eurl)!r}",
            )

    # Promise 2: parser-independent raw text host scan across the full body.
    parsed_host_keys = set(host_stats.keys())
    unparsed_stats: dict[str, dict[str, Any]] = {}
    scan_build_cache: dict[str, tuple[str, str | None, bool, bool] | None] = {}

    for raw_scanned, scan_cnt in _scan_raw_text_hosts(raw_html):
        if raw_scanned in scan_build_cache:
            built_s = scan_build_cache[raw_scanned]
        else:
            built_s = _build_host_entry(raw_scanned)
            scan_build_cache[raw_scanned] = built_s
        if built_s is None:
            continue
        key, written, is_overlong, is_num = built_s
        if is_overlong:
            add_tier5(
                "overlong_host",
                f"overlong link host ({len(raw_scanned)} chars, truncated): {key}",
            )
        elif is_num:
            add_tier5("numeric_host", f"numeric link host address: {key}")
        if key in parsed_host_keys:
            if written is not None and host_stats[key]["as_written"] is None:
                host_stats[key]["as_written"] = written
                if not raw_scanned.isascii():
                    add_tier5(
                        "idn_host",
                        f"non-ASCII or punycode link host: {key} (written as {written})",
                    )
            continue
        u_entry = unparsed_stats.get(key)
        if u_entry is None:
            u_entry = {
                "host": key,
                "count": 0,
                "anchor_count": 0,
                "embedded_count": 0,
                "mismatch_count": 0,
                "unparsed": True,
                "as_written": written,
            }
            unparsed_stats[key] = u_entry
        elif u_entry["as_written"] is None and written is not None:
            u_entry["as_written"] = written
        u_entry["count"] += scan_cnt

    if unparsed_stats:
        for key, u_entry in unparsed_stats.items():
            host_stats[key] = u_entry
        unparsed_names = sorted(unparsed_stats)
        shown_names = ", ".join(unparsed_names[:5])
        more_suffix = (
            f" (+{len(unparsed_names) - 5} more)"
            if len(unparsed_names) > 5
            else ""
        )
        outside_msg = (
            "body contains link targets outside normal markup: "
            f"{shown_names}{more_suffix}"
        )
        if outside_msg not in seen_warnings:
            seen_warnings.add(outside_msg)
            tier3_outside_markup.append(outside_msg)

    # Order links: mismatching links first, then other links (each in document order).
    ordered_links: list[dict[str, str]] = [
        {"text": txt, "href": hr}
        for txt, hr in distinct_pairs_order
        if (txt, hr) in mismatch_pairs
    ] + [
        {"text": txt, "href": hr}
        for txt, hr in distinct_pairs_order
        if (txt, hr) not in mismatch_pairs
    ]

    # Order hosts: mismatching link hosts -> other link hosts -> embedded -> unparsed.
    def _host_sort_key(k: str) -> tuple[int, str]:
        st = host_stats[k]
        if st["mismatch_count"] > 0:
            group = 0
        elif st["anchor_count"] > 0:
            group = 1
        elif st["embedded_count"] > 0:
            group = 2
        else:
            group = 3
        return (group, k)

    sorted_hosts: list[dict[str, Any]] = []
    for key in sorted(host_stats, key=_host_sort_key):
        st = host_stats[key]
        item: dict[str, Any] = {"host": st["host"], "count": st["count"]}
        if st["as_written"] is not None:
            item["as_written"] = st["as_written"]
        if st.get("unparsed"):
            item["unparsed"] = True
        elif st["anchor_count"] == 0 and st["embedded_count"] > 0:
            item["embedded"] = True
        sorted_hosts.append(item)

    if len(sorted_hosts) > MAX_PREVIEW_HOSTS:
        unverified = True
        omitted = len(sorted_hosts) - MAX_PREVIEW_HOSTS
        omit_msg = (
            f"{omitted} additional destination host(s) omitted "
            f"(total {len(sorted_hosts)} distinct hosts)"
        )
        if omit_msg not in seen_warnings:
            seen_warnings.add(omit_msg)
            tier4_omission.append(omit_msg)

    # Fold Tier 5 warnings beyond 3 per kind into a single count line.
    tier5_folded: list[str] = []
    tier5_raw_total = 0
    for kind, items in tier5_by_kind.items():
        tier5_raw_total += len(items)
        if len(items) <= 3:
            tier5_folded.extend(items)
        else:
            tier5_folded.extend(items[:3])
            label = _TIER5_KIND_LABELS.get(kind, kind)
            tier5_folded.append(f"{len(items) - 3} more {label} warning(s)")

    # Promise 3 order: unverified/size -> singular notices (omission, outside
    # normal markup) -> mismatch warnings (up to 100 + count line) -> folded Tier 5.
    singular_notices = tier4_omission + tier3_outside_markup
    if len(tier2_mismatch) <= MAX_MISMATCH_WARNINGS:
        mismatch_shown = tier2_mismatch
    else:
        unverified = True
        extra_mismatches = len(tier2_mismatch) - MAX_MISMATCH_WARNINGS
        mismatch_shown = [
            *tier2_mismatch[:MAX_MISMATCH_WARNINGS],
            f"{extra_mismatches} more mismatching link warning(s)",
        ]

    critical_warnings = tier1_unverified_size + singular_notices + mismatch_shown
    raw_warning_total = (
        len(tier1_unverified_size)
        + len(singular_notices)
        + len(tier2_mismatch)
        + tier5_raw_total
    )
    effective_cap = max(MAX_PREVIEW_WARNINGS, len(critical_warnings))
    combined_warnings = critical_warnings + tier5_folded
    shown_warnings = combined_warnings[:effective_cap]

    result: dict[str, Any] = {
        "links": ordered_links[:limit],
        "link_hosts": sorted_hosts[:MAX_PREVIEW_HOSTS],
        "warnings": shown_warnings,
    }
    if len(ordered_links) > limit:
        result["links_total"] = len(ordered_links)
    if len(sorted_hosts) > MAX_PREVIEW_HOSTS:
        result["link_hosts_total"] = len(sorted_hosts)
    if raw_warning_total > len(shown_warnings):
        result["warnings_total"] = raw_warning_total
    if unverified:
        result["links_unverified"] = True
    return result
