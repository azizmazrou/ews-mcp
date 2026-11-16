"""Deterministic, dependency-free email body cleaning (stdlib only).

Email bodies returned to an LLM are bloated by quoted reply chains,
signatures and disclaimers. This module strips them without any third
party dependency and without importing exchangelib. Bilingual: handles
both English and Arabic (Outlook / Gmail) reply conventions, and is
tolerant of RTL/LTR bidi control marks and ``>`` quote prefixes.

Public API:
    strip_quoted_history(text) -> (latest_reply_text, markers_stripped)
    strip_signature(text)      -> text without a trailing signature
    clean_body(text, max_chars) -> {"text", "quoted_blocks_stripped",
                                    "truncated", "original_chars"}
    html_to_text(html)         -> minimal readable plain text
"""

from __future__ import annotations

import re
from html import unescape
from typing import Callable

# --------------------------------------------------------------------------
# Line normalization helpers
# --------------------------------------------------------------------------

# Bidi / directionality control characters commonly injected by Outlook and
# Gmail around Arabic text (LRM, RLM, embeddings, isolates, ALM, BOM).
_BIDI_RE = re.compile(
    "[\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069\\u061c\\ufeff]"
)

# One or more leading '>' quote markers ("> ", ">> ", "> > " ...).
_QUOTE_PREFIX_RE = re.compile(r"^(?:>\s?)+")

# Normalize Arabic alef hamza variants so marker matching tolerates both
# spellings (e.g. "الإرسال" vs "الارسال", "أرسلت" vs "ارسلت").
_AR_TRANS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا"})


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _norm_line(line: str) -> tuple[str, bool]:
    """Return (normalized_line, was_quoted).

    Normalization: drop bidi control marks, strip surrounding whitespace,
    and remove leading '>' quote prefixes (recording that they were there).
    """
    s = _BIDI_RE.sub("", line).strip()
    m = _QUOTE_PREFIX_RE.match(s)
    quoted = m is not None
    if m:
        s = s[m.end():].strip()
    return s, quoted


# --------------------------------------------------------------------------
# Reply-history markers
# --------------------------------------------------------------------------

# "-----Original Message-----" separator (Outlook EN).
_ORIG_EN_RE = re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE)
# Arabic "رسالة أصلية" variants ("الرسالة الأصلية", dash-decorated, hamza
# normalized to bare alef before matching).
_ORIG_AR_RE = re.compile(r"^[-_*\s]*(?:ال)?رسالة\s+ال?اصلية[-_*\s:]*$")

# Outlook EN header block: "From:" paired with a following "Sent:"/"Date:".
_FROM_EN_RE = re.compile(r"^from\s*:", re.IGNORECASE)
_PAIR_EN_RE = re.compile(r"^(?:sent|date)\s*:", re.IGNORECASE)

# Outlook AR header block: "من:" paired with "تاريخ الإرسال:"/"التاريخ:"/
# "أرسلت:" (hamza-normalized).
_FROM_AR_RE = re.compile(r"^من\s*:")
_PAIR_AR_RE = re.compile(r"^(?:تاريخ\s+الارسال|التاريخ|ارسلت)\s*:")

# Gmail-style EN attribution: "On Mon, Jun 1, 2026 ... <a@b> wrote:".
_GMAIL_EN_RE = re.compile(r"^On .{4,80} wrote:\s*$", re.IGNORECASE)

# Gmail-style AR attribution heuristic ingredients (kept conservative).
_AR_WROTE_RE = re.compile(r"\bكتبت?\b")
_EMAIL_FRAG_RE = re.compile(r"<[^<>\s]+@[^<>\s]+>")
_DATEISH_RE = re.compile(r"\d{4}|\d{1,2}[/:\-]\d{1,2}")

# Minimum length of a run of '>'-prefixed lines treated as a quoted block.
_MIN_QUOTE_RUN = 3


def _find_markers(
    lines: list[str],
) -> tuple[list[tuple[int, str]], list[tuple[int, int]]]:
    """Return (markers, quote_runs).

    markers: sorted list of (line_index, kind) where kind is one of
        "original", "outlook_en", "outlook_ar", "gmail_en", "gmail_ar",
        "quote_run".
    quote_runs: list of (start_index, end_index) inclusive spans of runs of
        3+ consecutive '>'-quoted lines.
    """
    n = len(lines)
    norm = [_norm_line(ln) for ln in lines]
    markers: list[tuple[int, str]] = []
    runs: list[tuple[int, int]] = []

    # Runs of 3+ consecutive '>'-quoted lines.
    i = 0
    while i < n:
        if norm[i][1]:
            j = i
            while j < n and norm[j][1]:
                j += 1
            if j - i >= _MIN_QUOTE_RUN:
                runs.append((i, j - 1))
                markers.append((i, "quote_run"))
            i = j
        else:
            i += 1

    for idx in range(n):
        s = norm[idx][0]
        if not s:
            continue
        ar = s.translate(_AR_TRANS)
        if _ORIG_EN_RE.match(s) or _ORIG_AR_RE.match(ar):
            markers.append((idx, "original"))
            continue
        if _FROM_EN_RE.match(s) and any(
            _PAIR_EN_RE.match(norm[k][0]) for k in range(idx + 1, min(idx + 5, n))
        ):
            markers.append((idx, "outlook_en"))
            continue
        if _FROM_AR_RE.match(ar) and any(
            _PAIR_AR_RE.match(norm[k][0].translate(_AR_TRANS))
            for k in range(idx + 1, min(idx + 5, n))
        ):
            markers.append((idx, "outlook_ar"))
            continue
        if _GMAIL_EN_RE.match(s):
            markers.append((idx, "gmail_en"))
            continue
        # Conservative Arabic "wrote:" attribution: must contain the word
        # كتب/كتبت, end with ':' and carry a date-ish or <email> fragment.
        if (
            len(s) <= 200
            and s.endswith(":")
            and _AR_WROTE_RE.search(s)
            and (_EMAIL_FRAG_RE.search(s) or _DATEISH_RE.search(s))
        ):
            markers.append((idx, "gmail_ar"))

    markers.sort(key=lambda m: m[0])
    return markers, runs


def strip_quoted_history(text: str) -> tuple[str, int]:
    """Cut the body at the first reply-history marker.

    Returns (latest-reply-only text, count of quoted blocks/markers found in
    the stripped tail). A marker sitting on the *first* non-empty line is
    ignored (a body that genuinely starts with a marker-like line survives)
    — except an "Original Message" separator, which may cut even that
    early. The second non-empty line is NOT protected: a one-line reply
    directly followed by the quoted chain is the most common email shape.
    Trailing whitespace lines are stripped from the result.
    """
    if not text:
        return ("", 0)
    t = _normalize_newlines(text)
    lines = t.split("\n")
    markers, runs = _find_markers(lines)

    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    protected = set(nonempty[:1])

    cut: int | None = None
    for idx, kind in markers:
        if idx in protected and kind != "original":
            continue
        cut = idx
        break
    if cut is None:
        return (t.rstrip(), 0)

    # Count markers in the removed tail; markers that sit inside a counted
    # '>'-quoted run are absorbed by the run (counted once).
    run_spans = [(a, b) for (a, b) in runs if a >= cut]
    count = 0
    for idx, kind in markers:
        if idx < cut:
            continue
        if kind != "quote_run" and any(a <= idx <= b for (a, b) in run_spans):
            continue
        count += 1
    return ("\n".join(lines[:cut]).rstrip(), max(count, 1))


# --------------------------------------------------------------------------
# Signature stripping
# --------------------------------------------------------------------------

# (prefix, max length allowed AFTER the prefix on the same line).
# The remainder cap keeps lines like "Thanks, that works for me." safe.
_CLOSERS_EN: list[tuple[str, int]] = [
    ("best regards", 4),
    ("kind regards", 4),
    ("regards", 2),
    ("thanks,", 2),
    ("sent from my", 40),
]
# Matched against hamza-normalized text.
_CLOSERS_AR: list[tuple[str, int]] = [
    ("مع خالص التحية", 30),
    ("مع التحية", 30),
    ("تحياتي", 30),
    ("وتفضلوا بقبول", 60),
    ("ارسل من", 40),
]

_MAX_SIG_LINES = 6
_MAX_SIG_LINE_LEN = 80


def _is_closer_line(s: str) -> bool:
    cf = s.casefold()
    for prefix, max_rest in _CLOSERS_EN:
        if cf.startswith(prefix) and len(cf) - len(prefix) <= max_rest:
            return True
    ar = s.translate(_AR_TRANS)
    for prefix, max_rest in _CLOSERS_AR:
        if ar.startswith(prefix) and len(ar) - len(prefix) <= max_rest:
            return True
    return False


def strip_signature(text: str) -> str:
    """Remove a trailing signature block when one is confidently detected.

    Two detectors: the RFC "-- " delimiter line, and a trailing block of at
    most 6 short lines that begins with a common EN/AR closer. Conservative:
    only strips when at least 2 non-empty body lines remain above.
    """
    if not text:
        return ""
    t = _normalize_newlines(text).rstrip()
    if not t:
        return ""
    lines = t.split("\n")

    # RFC signature delimiter: a line that is exactly "--" / "-- ".
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip() == "--":
            above = sum(1 for ln in lines[:i] if ln.strip())
            below = sum(1 for ln in lines[i + 1:] if ln.strip())
            if above >= 2 and below <= 10:
                return "\n".join(lines[:i]).rstrip()
            break

    nonempty_idx = [i for i, ln in enumerate(lines) if ln.strip()]
    for i in nonempty_idx[-_MAX_SIG_LINES:]:
        s, _quoted = _norm_line(lines[i])
        if not _is_closer_line(s):
            continue
        block_ne = [ln for ln in lines[i:] if ln.strip()]
        if len(block_ne) > _MAX_SIG_LINES:
            continue
        if any(len(ln.strip()) > _MAX_SIG_LINE_LEN for ln in block_ne):
            continue
        if sum(1 for ln in lines[:i] if ln.strip()) < 2:
            continue
        return "\n".join(lines[:i]).rstrip()
    return t


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

_TRUNC_SUFFIX = "… [truncated]"
# 3+ consecutive blank lines (possibly whitespace-only) -> a single blank.
_BLANK_RUN_RE = re.compile(r"\n(?:[ \t]*\n){3,}")


def clean_body(text: str, max_chars: int = 4000) -> dict:
    """Full cleaning pipeline for an email body.

    normalize newlines -> strip quoted history -> strip signature ->
    collapse 3+ blank lines to 1 -> rstrip -> truncate at a word boundary.
    """
    raw = text or ""
    original_chars = len(raw)
    t = _normalize_newlines(raw)
    t, quoted_blocks = strip_quoted_history(t)
    t = strip_signature(t)
    t = _BLANK_RUN_RE.sub("\n\n", t)
    t = t.rstrip()

    truncated = False
    if max_chars and len(t) > max_chars:
        truncated = True
        budget = max(1, max_chars - len(_TRUNC_SUFFIX))
        cut = t[:budget]
        if budget < len(t) and not t[budget].isspace():
            # Mid-word: back up to the previous whitespace boundary.
            ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
            if ws > 0:
                cut = cut[:ws]
        t = cut.rstrip() + _TRUNC_SUFFIX

    return {
        "text": t,
        "quoted_blocks_stripped": quoted_blocks,
        "truncated": truncated,
        "original_chars": original_chars,
    }


# --------------------------------------------------------------------------
# Minimal HTML -> text (for reading)
# --------------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "tr", "li", "table",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_SKIP_TAGS = ("style", "script", "head")
_MAX_HREF_SHOWN = 80


def _norm_url(u: str) -> str:
    u = u.strip().casefold()
    u = re.sub(r"^(?:https?:)?//", "", u)
    u = re.sub(r"^mailto:", "", u)
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def _render_link(href: str, label: str) -> str:
    if not label:
        return href if href and len(href) <= _MAX_HREF_SHOWN else ""
    if not href or len(href) > _MAX_HREF_SHOWN or _norm_url(href) == _norm_url(label):
        return label
    return f"{label} ({href})"


_ASCII_LOWER_TRANS = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def _ascii_lower(s: str) -> str:
    return s.lower() if s.isascii() else s.translate(_ASCII_LOWER_TRANS)


_FOREIGN_ROOTS = frozenset({"svg", "math"})
_FOREIGN_BREAKOUT_TAGS = frozenset({
    "b", "big", "blockquote", "body", "br", "center", "code", "dd", "div",
    "dl", "dt", "em", "embed", "h1", "h2", "h3", "h4", "h5", "h6", "head",
    "hr", "i", "img", "li", "listing", "menu", "meta", "nobr", "ol", "p",
    "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup",
    "table", "tt", "u", "ul", "var",
})
_FOREIGN_BREAKOUT_END_TAGS = frozenset({"p", "br"})
_FONT_BREAKOUT_ATTRS = frozenset({"color", "face", "size"})
_HEAD_ALLOWED_TAGS = frozenset({
    "title", "meta", "link", "style", "script",
    "base", "basefont", "bgsound", "noscript", "noframes",
    "template", "html", "head",
})
_HEAD_CLOSE_END_TAGS = frozenset({"head", "body", "html"})
_NON_HTML_WS_RE = re.compile(r"[^ \t\n\r\x0c]")
_MARKUP_OPEN_RE = re.compile(r"<[A-Za-z/?!]")
_COMMENT_END_RE = re.compile(r"--!?>")
_TAG_NAME_RE = re.compile(r"[A-Za-z][^ \t\n\r\x0c/>]*+")
# Linear only as used below: _TAG_TAIL_RE via .match(s, j) at a tag (stopping
# the whole scan on None), and _ATTR_RE via .match(attr_slice, pos) stepping
# from one match end to the next and stopping at the first failure or non-advance
# (never .search() or .finditer(), which retry from every trailing space or '/').
_TAG_TAIL_RE = re.compile(
    r"""(?>(?:
        [ \t\n\r\x0c]++
      | /(?![>])
      | (?:=[^ \t\n\r\x0c/>=]*+|[^ \t\n\r\x0c/>=]++)
        [ \t\n\r\x0c]*+
        (?:=[ \t\n\r\x0c]*+
            (?:"[^"]*+"|'[^']*+'|(?![\"'])[^ \t\n\r\x0c>]*+)
        | (?!=)
        )
    )*+)
    (/?)>""",
    re.VERBOSE,
)
_ATTR_RE = re.compile(
    r"""[ \t\n\r\x0c/]*+
    (=[^ \t\n\r\x0c/>=]*+|[^ \t\n\r\x0c/>=]++)
    [ \t\n\r\x0c]*+
    (?:=[ \t\n\r\x0c]*+
        (?:"([^"]*+)"|'([^']*+)'|(?![\"'])([^ \t\n\r\x0c>]*+))
    )?+""",
    re.VERBOSE,
)


def _end_tag_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"</{name}(?=[ \t\n\r\x0c/>])", re.IGNORECASE | re.ASCII)


# HTML raw-text / RCDATA / script / plaintext elements -> (mode, end_re).
_RAWTEXT_SPECS: dict[str, tuple[str, re.Pattern[str] | None]] = {
    "script": ("script", None),
    "style": ("hide", _end_tag_re("style")),
    "title": ("hide", _end_tag_re("title")),
    "textarea": ("decode", _end_tag_re("textarea")),
    "xmp": ("literal", _end_tag_re("xmp")),
    "iframe": ("literal", _end_tag_re("iframe")),
    "noembed": ("literal", _end_tag_re("noembed")),
    "noframes": ("literal", _end_tag_re("noframes")),
    "plaintext": ("literal", None),
}
_SCRIPT_SCAN_RE = re.compile(
    r"<!--|-->|</?script(?=[ \t\n\r\x0c/>])",
    re.IGNORECASE | re.ASCII,
)
_OVERLONG_NUM_CHARREF_RE = re.compile(r"&#(?:[0-9]{9,}|[xX][0-9a-fA-F]{9,});?")
_COMPACT_CHUNK_CHARS = 65_536


def _replace_overlong_num_charref(match: re.Match[str]) -> str:
    tok = match.group(0)
    body = tok[2:-1] if tok.endswith(";") else tok[2:]
    if body[0] in ("x", "X"):
        digits = body[1:].lstrip("0") or "0"
        if len(digits) > 6:
            return "\ufffd"
        return f"&#x{digits};"
    digits = body.lstrip("0") or "0"
    if len(digits) > 7:
        return "\ufffd"
    return f"&#{digits};"


def _unescape_charrefs(s: str) -> str:
    if "&" not in s:
        return s
    if "&#" in s and _OVERLONG_NUM_CHARREF_RE.search(s) is not None:
        s = _OVERLONG_NUM_CHARREF_RE.sub(_replace_overlong_num_charref, s)
    return unescape(s)


def _extract_href(attr_slice: str) -> str:
    if not attr_slice or "href" not in _ascii_lower(attr_slice):
        return ""
    pos = 0
    end = len(attr_slice)
    while pos < end:
        m = _ATTR_RE.match(attr_slice, pos)
        if m is None or m.end() <= pos:
            break
        pos = m.end()
        if _ascii_lower(m.group(1)) == "href":
            raw_val = m.group(2) or m.group(3) or m.group(4)
            if not raw_val:
                return ""
            if "\x00" in raw_val:
                raw_val = raw_val.replace("\x00", "")
            return _unescape_charrefs(raw_val).strip()
    return ""


def _font_has_breakout_attr(attr_slice: str) -> bool:
    if not attr_slice:
        return False
    low = _ascii_lower(attr_slice)
    if "color" not in low and "face" not in low and "size" not in low:
        return False
    pos = 0
    end = len(attr_slice)
    while pos < end:
        m = _ATTR_RE.match(attr_slice, pos)
        if m is None or m.end() <= pos:
            break
        pos = m.end()
        if _ascii_lower(m.group(1)) in _FONT_BREAKOUT_ATTRS:
            return True
    return False


def html_to_text_bounded(
    html: str,
    *,
    max_chars: int | None = None,
    transform: Callable[[str], str] | None = None,
) -> tuple[str, bool]:
    """Single-pass linear-time HTML -> plain text converter; never raises.

    When ``max_chars`` is given, stops assembling once ``max_chars`` output
    characters have been produced and returns ``(text, was_truncated)``.
    """
    if not html:
        return "", False
    s = html

    limit = max(1, int(max_chars)) if max_chars is not None else None
    out_parts: list[str] = []
    out_chars = 0
    truncated = False
    has_output = False
    pending_blank = False

    line_ready: list[str] = []
    line_ready_len = 0
    line_tail_space = False
    cur_raw: list[str] = []
    cur_raw_len = 0

    in_head = False
    # Foreign integration points (foreignObject, desc, mtext, annotation-xml, etc.) are not modelled.
    foreign_stack: list[str] = []
    foreign_counts = {"svg": 0, "math": 0}
    link_href = ""
    in_link = False
    link_ready: list[str] = []
    link_ready_len = 0
    link_tail_space = False
    link_raw: list[str] = []
    link_raw_len = 0

    def commit_collapsed_line(collapsed: str) -> None:
        nonlocal out_chars, truncated, has_output, pending_blank
        if truncated:
            return
        if not collapsed:
            if has_output:
                pending_blank = True
            return
        sep = ("\n\n" if pending_blank else "\n") if has_output else ""
        if limit is not None:
            needed = len(sep) + len(collapsed)
            remaining = limit - out_chars
            if needed > remaining:
                if remaining > 0:
                    out_parts.append((sep + collapsed)[:remaining])
                    out_chars = limit
                truncated = True
                has_output = True
                pending_blank = False
                return
        if sep:
            out_parts.append(sep)
            out_chars += len(sep)
        out_parts.append(collapsed)
        out_chars += len(collapsed)
        has_output = True
        pending_blank = False

    def fold_cur_raw(*, check_limit: bool) -> None:
        nonlocal cur_raw_len, line_ready_len, line_tail_space
        if not cur_raw:
            return
        chunk = "".join(cur_raw)
        cur_raw.clear()
        cur_raw_len = 0
        if transform is not None:
            chunk = transform(chunk)
        if not chunk:
            return
        leading_ws = chunk[0].isspace()
        trailing_ws = chunk[-1].isspace()
        words = chunk.split()
        if not words:
            if line_ready:
                line_tail_space = True
            return
        piece = " ".join(words)
        if line_ready and (line_tail_space or leading_ws):
            line_ready.append(" ")
            line_ready_len += 1
        line_ready.append(piece)
        line_ready_len += len(piece)
        line_tail_space = trailing_ws
        if check_limit and limit is not None:
            sep_len = (2 if pending_blank else 1) if has_output else 0
            if out_chars + sep_len + line_ready_len > limit:
                collapsed = "".join(line_ready)
                line_ready.clear()
                line_ready_len = 0
                line_tail_space = False
                commit_collapsed_line(collapsed)

    def flush_raw_line() -> None:
        nonlocal cur_raw_len, line_ready_len, line_tail_space
        if truncated:
            return
        if not line_ready:
            if not cur_raw:
                commit_collapsed_line("")
                return
            raw_ln = "".join(cur_raw)
            cur_raw.clear()
            cur_raw_len = 0
            if transform is not None:
                raw_ln = transform(raw_ln)
            commit_collapsed_line(" ".join(raw_ln.split()))
            return
        fold_cur_raw(check_limit=False)
        collapsed = "".join(line_ready)
        line_ready.clear()
        line_ready_len = 0
        line_tail_space = False
        commit_collapsed_line(collapsed)

    def fold_link_raw(*, check_limit: bool) -> None:
        nonlocal link_raw_len, link_ready_len, link_tail_space
        if not link_raw:
            return
        chunk = "".join(link_raw)
        link_raw.clear()
        link_raw_len = 0
        if transform is not None:
            chunk = transform(chunk)
        if not chunk:
            return
        leading_ws = chunk[0].isspace()
        trailing_ws = chunk[-1].isspace()
        words = chunk.split()
        if not words:
            if link_ready:
                link_tail_space = True
            return
        piece = " ".join(words)
        if link_ready and (link_tail_space or leading_ws):
            link_ready.append(" ")
            link_ready_len += 1
        link_ready.append(piece)
        link_ready_len += len(piece)
        link_tail_space = trailing_ws
        if check_limit and limit is not None:
            if out_chars + line_ready_len + link_ready_len > limit:
                flush_link()
                if not truncated:
                    fold_cur_raw(check_limit=True)

    def emit_out(seg: str) -> None:
        nonlocal cur_raw_len
        if not seg or truncated:
            return
        if "\n" not in seg:
            cur_raw.append(seg)
            cur_raw_len += len(seg)
            if limit is not None and cur_raw_len >= _COMPACT_CHUNK_CHARS:
                fold_cur_raw(check_limit=True)
            return
        parts = seg.split("\n")
        last_idx = len(parts) - 1
        for idx, part in enumerate(parts):
            if part:
                cur_raw.append(part)
                cur_raw_len += len(part)
                if limit is not None and cur_raw_len >= _COMPACT_CHUNK_CHARS:
                    fold_cur_raw(check_limit=True)
                    if truncated:
                        return
            if idx < last_idx:
                flush_raw_line()
                if truncated:
                    return

    def flush_link() -> None:
        nonlocal link_href, in_link, link_raw_len, link_ready_len, link_tail_space
        if not in_link:
            return
        if not link_ready:
            raw_lbl = "".join(link_raw)
            link_raw.clear()
            link_raw_len = 0
            if transform is not None and raw_lbl:
                raw_lbl = transform(raw_lbl)
            label = " ".join(raw_lbl.split()) if raw_lbl else ""
        else:
            fold_link_raw(check_limit=False)
            label = "".join(link_ready)
            link_ready.clear()
            link_ready_len = 0
            link_tail_space = False
        href = link_href
        link_href = ""
        in_link = False
        if truncated:
            return
        rendered = _render_link(href, label)
        if rendered:
            emit_out(rendered)

    def append_piece(seg: str) -> None:
        nonlocal link_raw_len
        if "\x00" in seg:
            seg = seg.replace("\x00", "")
        if not seg or truncated:
            return
        if in_link:
            link_raw.append(seg)
            link_raw_len += len(seg)
            if limit is not None and link_raw_len >= _COMPACT_CHUNK_CHARS:
                fold_link_raw(check_limit=True)
        else:
            emit_out(seg)

    def append_newline() -> None:
        nonlocal link_raw_len
        if truncated:
            return
        if in_link:
            link_raw.append(" ")
            link_raw_len += 1
            if limit is not None and link_raw_len >= _COMPACT_CHUNK_CHARS:
                fold_link_raw(check_limit=True)
        else:
            flush_raw_line()

    n = len(s)
    i = 0

    def clear_foreign() -> None:
        foreign_stack.clear()
        foreign_counts["svg"] = 0
        foreign_counts["math"] = 0

    def emit_raw_segment(raw_seg: str) -> None:
        nonlocal in_head
        if in_head:
            ws_m = _NON_HTML_WS_RE.search(raw_seg)
            if ws_m is None:
                return
            in_head = False
            raw_seg = raw_seg[ws_m.start() :]
        append_piece(_unescape_charrefs(raw_seg))

    def find_rawtext_end(end_re: re.Pattern[str], pos: int) -> tuple[int, int | None]:
        cm = end_re.search(s, pos)
        if cm is None:
            return n, None
        after_close = cm.end()
        if after_close < n and s[after_close] == ">":
            return cm.start(), after_close + 1
        tm_close = _TAG_TAIL_RE.match(s, after_close)
        return cm.start(), (tm_close.end() if tm_close is not None else None)

    def scan_script_end(pos: int) -> int | None:
        # 0 = plain script data, 1 = escaped, 2 = double-escaped.
        state = 0
        while pos < n:
            sm = _SCRIPT_SCAN_RE.search(s, pos)
            if sm is None:
                return None
            c1 = sm.group(0)[1]
            if c1 == "!":  # <!--
                k = sm.end()
                while k < n and s[k] == "-":
                    k += 1
                if k < n and s[k] == ">":
                    state = 0
                    pos = k + 1
                else:
                    if state == 0:
                        state = 1
                    pos = k
            elif c1 == "-":  # -->
                state = 0
                pos = sm.end()
            elif c1 == "/":  # </script(?=[ \t\n\r\x0c/>])
                if state == 2:
                    state = 1
                    pos = sm.end() + 1
                else:
                    after_close = sm.end()
                    if after_close < n and s[after_close] == ">":
                        return after_close + 1
                    tm_close = _TAG_TAIL_RE.match(s, after_close)
                    return tm_close.end() if tm_close is not None else None
            else:  # <script(?=[ \t\n\r\x0c/>])
                if state == 1:
                    state = 2
                pos = sm.end() + 1
        return None

    while i < n and not truncated:
        m = _MARKUP_OPEN_RE.search(s, i)
        if m is None:
            emit_raw_segment(s[i:n])
            break
        lt = m.start()
        if lt > i:
            emit_raw_segment(s[i:lt])
            if truncated:
                break

        nxt = s[lt + 1]

        # 1. Comments, foreign CDATA, DOCTYPE, conditional/bogus declarations: <!...>
        if nxt == "!":
            if s.startswith("<!--", lt):
                if s.startswith("<!-->", lt):
                    i = lt + 5
                    continue
                if s.startswith("<!--->", lt):
                    i = lt + 6
                    continue
                end_m = _COMMENT_END_RE.search(s, lt + 4)
                if end_m is None:
                    break
                i = end_m.end()
                continue
            if foreign_stack and s.startswith("<![CDATA[", lt):
                end_cd = s.find("]]>", lt + 9)
                if end_cd == -1:
                    append_piece(s[lt + 9 : n])
                    break
                append_piece(s[lt + 9 : end_cd])
                i = end_cd + 3
                continue
            gt = s.find(">", lt + 2)
            if gt == -1:
                break
            i = gt + 1
            continue

        # 2. Processing instructions / XML declarations: <?...>
        if nxt == "?":
            gt = s.find(">", lt + 2)
            if gt == -1:
                break
            i = gt + 1
            continue

        # 3. End tags: </...>
        if nxt == "/":
            if lt + 2 >= n:
                in_head = False
                append_piece("</")
                break
            c2 = s[lt + 2]
            if not ("A" <= c2 <= "Z" or "a" <= c2 <= "z"):
                if c2 == ">":
                    i = lt + 3
                    continue
                gt = s.find(">", lt + 2)
                if gt == -1:
                    break
                i = gt + 1
                continue
            nm = _TAG_NAME_RE.match(s, lt + 2)
            j = nm.end() if nm else lt + 3
            end_tag = _ascii_lower(s[lt + 2 : j])
            if j < n and s[j] == ">":
                i = j + 1
            else:
                tm = _TAG_TAIL_RE.match(s, j)
                if tm is None:
                    break
                i = tm.end()
            if end_tag in _FOREIGN_ROOTS:
                if foreign_counts[end_tag] > 0:
                    while True:
                        popped = foreign_stack.pop()
                        foreign_counts[popped] -= 1
                        if popped == end_tag:
                            break
                continue
            if foreign_stack and end_tag in _FOREIGN_BREAKOUT_END_TAGS:
                clear_foreign()
            if in_head:
                if end_tag in _HEAD_CLOSE_END_TAGS:
                    in_head = False
                continue
            if end_tag == "a":
                flush_link()
                continue
            if end_tag in ("td", "th"):
                append_piece(" ")
                continue
            if end_tag in _BLOCK_TAGS:
                append_newline()
            continue

        # 4. Start tags: <[A-Za-z]...
        nm = _TAG_NAME_RE.match(s, lt + 1)
        j = nm.end() if nm else lt + 2
        tag = _ascii_lower(s[lt + 1 : j])
        if j < n and s[j] == ">":
            attr_slice = ""
            raw_self_closing = False
            i = j + 1
        else:
            tm = _TAG_TAIL_RE.match(s, j)
            if tm is None:
                break
            attr_slice = s[j : tm.start(1)]
            raw_self_closing = bool(tm.group(1))
            i = tm.end()

        if foreign_stack and (
            tag in _FOREIGN_BREAKOUT_TAGS
            or (tag == "font" and _font_has_breakout_attr(attr_slice))
        ):
            clear_foreign()

        if in_head:
            if tag in _HEAD_ALLOWED_TAGS:
                spec = _RAWTEXT_SPECS.get(tag)
                if spec is not None:
                    mode, end_re = spec
                    if mode == "script":
                        next_i = scan_script_end(i)
                    elif end_re is not None:
                        _, next_i = find_rawtext_end(end_re, i)
                    else:
                        next_i = None
                    if next_i is None:
                        break
                    i = next_i
                continue
            in_head = False

        if tag in _FOREIGN_ROOTS:
            if not raw_self_closing:
                foreign_stack.append(tag)
                foreign_counts[tag] += 1
            continue

        if not foreign_stack and tag in _RAWTEXT_SPECS:
            mode, end_re = _RAWTEXT_SPECS[tag]
            if mode == "script":
                next_i = scan_script_end(i)
                if next_i is None:
                    break
                i = next_i
                continue
            if end_re is None:
                append_piece(s[i:n])
                break
            content_end, next_i = find_rawtext_end(end_re, i)
            if mode == "decode":
                append_piece(_unescape_charrefs(s[i:content_end]))
            elif mode == "literal":
                append_piece(s[i:content_end])
            if next_i is None:
                break
            i = next_i
            continue

        if tag == "head" and not foreign_stack:
            in_head = True
            continue

        if tag == "br":
            append_newline()
            continue

        if tag in _BLOCK_TAGS:
            append_newline()
            if truncated:
                break

        if tag == "a" and not (raw_self_closing and bool(foreign_stack)):
            flush_link()
            if truncated:
                break
            link_href = _extract_href(attr_slice)
            in_link = True

    if not truncated:
        flush_link()
    if not truncated:
        flush_raw_line()
    result = "".join(out_parts)
    if truncated:
        result = result.rstrip("\n")
    return result, truncated


def html_to_text(html: str) -> str:
    """Minimal stdlib HTML -> text conversion for reading.

    Block tags (p, div, br, tr, li, h1-h6, table) emit newlines; style /
    script / head contents are dropped; entities are unescaped; links keep
    their href only when it is short and differs from the label. Whitespace
    is collapsed per line; Arabic text passes through untouched.
    """
    if not html:
        return ""
    text, _ = html_to_text_bounded(html)
    return text
