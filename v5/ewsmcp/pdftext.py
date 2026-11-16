"""Arabic/bidirectional PDF reading-order recovery and /ToUnicode ligature repair.

Standard library only, except for public ``pypdf.generic`` stream/name objects
used when rewriting multi-letter right-to-left ``/ToUnicode`` CMap entries in
memory prior to extraction.

Guarantees:
- Pages containing no right-to-left letters are returned byte-identical to
  ``pypdf`` layout mode.
- Lines longer than ``MAX_LINE_CHARS`` (5,000) after collapsing spaces are
  returned unconverted with a note.
- Every step is strictly linear in line length or bounded by a small constant
  (at most ``MAX_WORD_REPAIR_PIECES = 12`` pieces in word repair and
  ``MAX_SPLIT_TOKEN_PIECES = 8`` pieces in split-token joining).
- Public entry points never raise on any input: malformed fonts are skipped, and
  any unexpected internal error returns the page's layout text with a note.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

# --- Named cost and safety bounds (every constant has unit tests) ------------

MAX_TOUNICODE_BYTES = 2_000_000
MAX_CODESPACE_HEX_CHARS = 4
MAX_BFRANGE_EXPAND = 256
MAX_LINE_CHARS = 5_000
MAX_WORD_REPAIR_PIECES = 12
MAX_SPLIT_TOKEN_PIECES = 8
MAX_TIGHT_SEP_CHARS = 3

# --- Character tables & compiled patterns ------------------------------------

_PRES_RE = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFC]+")
_RTL_SCRIPT_HINT_RE = re.compile(
    r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFC"
    r"\U00010800-\U00010FFF\U0001E800-\U0001EEFF]"
)
_TWIN_BASE_RE = re.compile(r"[\u06CC\u06BE\u06A9]")
_WIDE_SPACE_CAP_RE = re.compile(r" {7,}")
_ANY_SPACES_RE = re.compile(r" {2,}")
_SPLIT_SPACES_RE = re.compile(r"( +)")
_NUM_SEP_L_RE = re.compile(r"(?<=\d) {1,5}(?=[-/] {0,5}\d)")
_NUM_SEP_R_RE = re.compile(r"(?<=\d[-/]) {1,5}(?=\d)")
_WORD_PRODUCER_RE = re.compile(r"\bword\b", re.IGNORECASE)

_OPEN_BRACKETS = "([{«"
_CLOSE_BRACKETS = ")]}»"
_PARTNER: Dict[str, str] = dict(
    zip(_OPEN_BRACKETS + _CLOSE_BRACKETS, _CLOSE_BRACKETS + _OPEN_BRACKETS)
)

_FORMAT_CHARS = (
    "\u061c"  # ARABIC LETTER MARK
    "\u200e\u200f"  # LRM, RLM
    "\u202a\u202b\u202c\u202d\u202e"  # LRE, RLE, PDF, LRO, RLO
    "\u2066\u2067\u2068\u2069"  # LRI, RLI, FSI, PDI
)
_STRIP_FORMAT_TABLE = str.maketrans("", "", _FORMAT_CHARS)

_TIGHT_CLASSES = frozenset({"ES", "CS", "ET"})
_TIGHT_ON_CHARS = frozenset("_/\\#@&=?~")

# Letters specific to Persian, Urdu, and related languages: when any appears on
# a page, Persian/Urdu forms of yeh, heh, and kaf are genuine and are not folded.
_NON_ARABIC_LETTERS = frozenset(
    "\u067e\u0686\u0698\u06af\u0679\u0688\u0691\u06ba\u06d2\u06c1\u06d5"
)

# Presentation forms of Persian yeh, heh, and kaf shared by Arabic fonts:
# initial/medial Farsi yeh -> Arabic yeh (U+064A); isolated/final -> alef maksura (U+0649).
_TWIN_FORMS: Dict[int, int] = {
    0xFBFE: 0x064A,
    0xFBFF: 0x064A,
    0xFBFC: 0x0649,
    0xFBFD: 0x0649,
    0xFBAA: 0x0647,
    0xFBAB: 0x0647,
    0xFBAC: 0x0647,
    0xFBAD: 0x0647,
    0xFB8E: 0x0643,
    0xFB8F: 0x0643,
    0xFB90: 0x0643,
    0xFB91: 0x0643,
}

_AR_CHAR_CLASS = (
    r"[\u0621-\u065F\u066E-\u06D3\u06D5-\u06DC\u06DF-\u06E8"
    r"\u06EA-\u06EF\u06FA-\u06FF\u0750-\u077F\u08A0-\u08FF]"
)
_WORD_RE = re.compile(_AR_CHAR_CLASS + "+")

# Per-codepoint memo caches (bounded by distinct characters seen; Unicode is finite).
_BIDI_CACHE: Dict[str, str] = {}
_LETTER_KIND_CACHE: Dict[str, int] = {}  # 1 = RTL letter, 2 = LTR letter, 0 = other


def _cls(ch: str) -> str:
    cached = _BIDI_CACHE.get(ch)
    if cached is not None:
        return cached
    val = unicodedata.bidirectional(ch) or "ON"
    if len(_BIDI_CACHE) < 65_536:
        _BIDI_CACHE[ch] = val
    return val


def _letter_kind(ch: str) -> int:
    cached = _LETTER_KIND_CACHE.get(ch)
    if cached is not None:
        return cached
    bc = _cls(ch)
    if bc in ("R", "AL") and unicodedata.category(ch).startswith("L"):
        kind = 1
    elif bc == "L" and unicodedata.category(ch).startswith("L"):
        kind = 2
    else:
        kind = 0
    if len(_LETTER_KIND_CACHE) < 65_536:
        _LETTER_KIND_CACHE[ch] = kind
    return kind


def _is_rtl_letter(ch: str) -> bool:
    return _letter_kind(ch) == 1


def _is_ltr_letter(ch: str) -> bool:
    return _letter_kind(ch) == 2


def letter_counts(text: str) -> Tuple[int, int]:
    """Return ``(rtl_letter_count, ltr_letter_count)`` for ``text``."""
    if not text:
        return 0, 0
    rtl = 0
    ltr = 0
    lk_get = _LETTER_KIND_CACHE.get
    for ch in text:
        kind = lk_get(ch)
        if kind is None:
            kind = _letter_kind(ch)
        if kind == 1:
            rtl += 1
        elif kind == 2:
            ltr += 1
    return rtl, ltr


def has_rtl_letters(text: Any) -> bool:
    """Return True if ``text`` contains at least one right-to-left letter."""
    if not isinstance(text, str) or not text:
        return False
    if not _RTL_SCRIPT_HINT_RE.search(text):
        return False
    lk_get = _LETTER_KIND_CACHE.get
    for ch in text:
        kind = lk_get(ch)
        if kind is None:
            kind = _letter_kind(ch)
        if kind == 1:
            return True
    return False


# ---------------------------------------------------------------------------
# 1. /ToUnicode multi-letter RTL ligature reversal (before text extraction)
# ---------------------------------------------------------------------------


_CMAP_DELIMS = frozenset(" \t\r\n\x0c\x00<>[]()/{}%")


def _scan_cmap_tokens(text: str) -> Iterator[Tuple[str, str, int, int]]:
    """Linear single-pass tokenizer for Adobe /ToUnicode CMap streams."""
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in " \t\r\n\x0c\x00":
            i += 1
            continue
        if ch == "%":
            nl = text.find("\n", i + 1)
            cr = text.find("\r", i + 1)
            if nl == -1 and cr == -1:
                break
            if nl == -1:
                i = cr + 1
            elif cr == -1:
                i = nl + 1
            else:
                i = (nl if nl < cr else cr) + 1
            continue
        if ch == "<":
            if i + 1 < n and text[i + 1] == "<":
                i += 2
                continue
            end = text.find(">", i + 1)
            if end == -1:
                yield ("hex", text[i + 1 :], i, n)
                break
            yield ("hex", text[i + 1 : end], i, end + 1)
            i = end + 1
            continue
        if ch == ">":
            i += 2 if (i + 1 < n and text[i + 1] == ">") else 1
            continue
        if ch in "[]":
            yield (ch, ch, i, i + 1)
            i += 1
            continue
        if ch == "(":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            i = j
            continue
        if ch in ")/{}":
            i += 1
            continue
        j = i + 1
        while j < n and text[j] not in _CMAP_DELIMS:
            j += 1
        yield ("word", text[i:j], i, j)
        i = j


def _reverse_rtl_hex_string(raw_hex: str) -> Optional[str]:
    """Return reversed UTF-16BE hex if ``raw_hex`` is a multi-char RTL string."""
    if len(raw_hex) < 8 or len(raw_hex) % 4 != 0:
        return None
    try:
        txt = bytes.fromhex(raw_hex).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return None
    if len(txt) < 2 or not all(_cls(c) in ("R", "AL", "NSM") for c in txt):
        return None
    return txt[::-1].encode("utf-16-be").hex().upper()


def _codespace_wider_than_two_bytes(cmap_text: str) -> bool:
    in_codespace = False
    for kind, val, _s, _e in _scan_cmap_tokens(cmap_text):
        if kind == "word":
            if val == "begincodespacerange":
                in_codespace = True
            elif val == "endcodespacerange":
                in_codespace = False
        elif in_codespace and kind == "hex":
            if len("".join(val.split())) > MAX_CODESPACE_HEX_CHARS:
                return True
    return False


def _expand_bfrange_start_string(
    lo_raw: str,
    hi_raw: str,
    dst_raw: str,
) -> Optional[str]:
    """Expand ``<lo> <hi> <dstStart>`` into ``[<rev0> ...]`` when <= 256 RTL ligatures."""
    lo_hex = "".join(lo_raw.split())
    hi_hex = "".join(hi_raw.split())
    dst_hex = "".join(dst_raw.split())
    if (
        not lo_hex
        or len(lo_hex) != len(hi_hex)
        or len(lo_hex) % 2 != 0
        or len(lo_hex) > MAX_CODESPACE_HEX_CHARS
        or len(dst_hex) < 8
        or len(dst_hex) % 4 != 0
    ):
        return None
    try:
        lo_val = int(lo_hex, 16)
        hi_val = int(hi_hex, 16)
        # Validate dst_hex is valid hex and extract its trailing 16-bit unit.
        bytes.fromhex(dst_hex)
        last_unit = int(dst_hex[-4:], 16)
    except ValueError:
        return None

    count = hi_val - lo_val + 1
    if count < 1 or count > MAX_BFRANGE_EXPAND or (last_unit + count - 1) > 0xFFFF:
        return None

    prefix = dst_hex[:-4].upper()
    expanded: List[str] = []
    any_reversed = False
    for offset in range(count):
        cand_hex = f"{prefix}{last_unit + offset:04X}"
        rev = _reverse_rtl_hex_string(cand_hex)
        if rev is not None:
            expanded.append(f"<{rev}>")
            any_reversed = True
        else:
            expanded.append(f"<{cand_hex}>")
    if not any_reversed:
        return None
    return "[" + " ".join(expanded) + "]"


def _rewrite_cmap_ligatures(cmap_text: str) -> str:
    """Rewrite multi-letter RTL entries in ``bfchar`` and ``bfrange`` sections."""
    edits: List[Tuple[int, int, str]] = []
    section: Optional[str] = None
    bfchar_step = 0
    bfrange_step = 0
    lo_raw = ""
    hi_raw = ""

    for kind, val, start, end in _scan_cmap_tokens(cmap_text):
        if kind == "word":
            if val == "beginbfchar":
                section = "bfchar"
                bfchar_step = 0
            elif val == "endbfchar":
                section = None
            elif val == "beginbfrange":
                section = "bfrange"
                bfrange_step = 0
            elif val == "endbfrange":
                section = None
            continue

        if section == "bfchar":
            if kind == "hex":
                if bfchar_step == 0:
                    bfchar_step = 1
                else:
                    bfchar_step = 0
                    rev = _reverse_rtl_hex_string("".join(val.split()))
                    if rev is not None:
                        edits.append((start, end, f"<{rev}>"))
            else:
                bfchar_step = 0
        elif section == "bfrange":
            if bfrange_step == 0:
                if kind == "hex":
                    lo_raw = val
                    bfrange_step = 1
            elif bfrange_step == 1:
                if kind == "hex":
                    hi_raw = val
                    bfrange_step = 2
                else:
                    bfrange_step = 0
            elif bfrange_step == 2:
                if kind == "[":
                    bfrange_step = 3
                elif kind == "hex":
                    arr = _expand_bfrange_start_string(lo_raw, hi_raw, val)
                    if arr is not None:
                        lead = " " if (start > 0 and not cmap_text[start - 1].isspace()) else ""
                        edits.append((start, end, lead + arr))
                    bfrange_step = 0
                else:
                    bfrange_step = 0
            elif bfrange_step == 3:
                if kind == "]":
                    bfrange_step = 0
                elif kind == "hex":
                    rev = _reverse_rtl_hex_string("".join(val.split()))
                    if rev is not None:
                        edits.append((start, end, f"<{rev}>"))

    if not edits:
        return cmap_text
    chunks: List[str] = []
    pos = 0
    for s_idx, e_idx, repl in edits:
        chunks.append(cmap_text[pos:s_idx])
        chunks.append(repl)
        pos = e_idx
    chunks.append(cmap_text[pos:])
    return "".join(chunks)


def _patch_font_dict_tounicode(
    fonts_obj: Any,
    done: Set[Any],
    decoded_stream_cls: Any,
    name_obj_cls: Any,
) -> int:
    changed = 0
    if not hasattr(fonts_obj, "items"):
        return 0
    for _font_key, ref in list(fonts_obj.items()):
        try:
            font = ref.get_object() if hasattr(ref, "get_object") else ref
            if font is None or not hasattr(font, "get"):
                continue
            ident = getattr(ref, "idnum", None)
            key = ("idnum", ident) if ident is not None else ("id", id(font))
            if key in done:
                continue
            done.add(key)

            tu = font.get("/ToUnicode")
            if tu is None:
                continue
            tu_obj = tu.get_object() if hasattr(tu, "get_object") else tu
            if not hasattr(tu_obj, "get_data"):
                continue
            data = tu_obj.get_data()
            if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_TOUNICODE_BYTES:
                continue

            text = bytes(data).decode("latin-1")
            if _codespace_wider_than_two_bytes(text):
                continue

            new_text = _rewrite_cmap_ligatures(text)
            if new_text != text:
                stream = decoded_stream_cls()
                stream.set_data(new_text.encode("latin-1"))
                font[name_obj_cls("/ToUnicode")] = stream
                changed += 1

            # Type0 composite fonts may also carry DescendantFonts dictionaries.
            desc = font.get("/DescendantFonts")
            desc_obj = desc.get_object() if hasattr(desc, "get_object") else desc
            if isinstance(desc_obj, list):
                for idx, dref in enumerate(desc_obj):
                    changed += _patch_font_dict_tounicode(
                        {str(idx): dref}, done, decoded_stream_cls, name_obj_cls
                    )
        except MemoryError:
            raise
        except Exception:
            continue
    return changed


def fix_ligature_maps(reader: Any) -> int:
    """Rewrite multi-letter RTL ``/ToUnicode`` entries to visual order in memory.

    Returns the number of font ``/ToUnicode`` streams updated. Never raises on
    malformed PDFs, oversized maps (> ``MAX_TOUNICODE_BYTES``), or >2-byte code
    spaces; any font that cannot be handled is left untouched.
    """
    try:
        from pypdf.generic import DecodedStreamObject, NameObject
    except Exception:
        return 0

    try:
        pages = getattr(reader, "pages", None)
        if pages is None:
            return 0
        page_count = len(pages)
    except MemoryError:
        raise
    except Exception:
        return 0

    done_fonts: Set[Any] = set()
    visited_resources: Set[Any] = set()
    changed = 0

    for idx in range(page_count):
        try:
            page = pages[idx]
            res_stack: List[Any] = [page.get("/Resources")]
            while res_stack:
                res_ref = res_stack.pop()
                if res_ref is None:
                    continue
                res_ident = getattr(res_ref, "idnum", None)
                res_obj = (
                    res_ref.get_object() if hasattr(res_ref, "get_object") else res_ref
                )
                if res_obj is None or not hasattr(res_obj, "get"):
                    continue
                rkey = ("idnum", res_ident) if res_ident is not None else ("id", id(res_obj))
                if rkey in visited_resources:
                    continue
                visited_resources.add(rkey)

                fonts_ref = res_obj.get("/Font")
                fonts_obj = (
                    fonts_ref.get_object()
                    if hasattr(fonts_ref, "get_object")
                    else fonts_ref
                )
                if fonts_obj is not None:
                    changed += _patch_font_dict_tounicode(
                        fonts_obj, done_fonts, DecodedStreamObject, NameObject
                    )

                xobj_ref = res_obj.get("/XObject")
                xobj_dict = (
                    xobj_ref.get_object()
                    if hasattr(xobj_ref, "get_object")
                    else xobj_ref
                )
                if hasattr(xobj_dict, "values"):
                    for xref in list(xobj_dict.values()):
                        try:
                            xobj = xref.get_object() if hasattr(xref, "get_object") else xref
                            if hasattr(xobj, "get") and xobj.get("/Resources") is not None:
                                res_stack.append(xobj.get("/Resources"))
                        except Exception:
                            continue
        except MemoryError:
            raise
        except Exception:
            continue

    return changed


# ---------------------------------------------------------------------------
# 2. Layout producer detection (Word vs Unicode rules)
# ---------------------------------------------------------------------------


def laid_out_by_word(reader: Any) -> bool:
    """Return True when Producer or Creator metadata contains whole-word ``Word``."""
    try:
        meta = getattr(reader, "metadata", None) or {}
        producer = meta.get("/Producer") or getattr(meta, "producer", None) or ""
        creator = meta.get("/Creator") or getattr(meta, "creator", None) or ""
        return bool(_WORD_PRODUCER_RE.search(f"{producer} {creator}"))
    except MemoryError:
        raise
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 3. Presentation forms, Persian twins, token joining & word repair
# ---------------------------------------------------------------------------


def normalize_arabic_presentation_forms(text: str, *, fold_twins: bool = False) -> str:
    """Map Arabic Presentation Forms (U+FB50–U+FDFF, U+FE70–U+FEFC) to base letters.

    When ``fold_twins`` is True, Persian twin presentation forms for yeh, heh,
    and kaf are mapped by glyph positional form before ``NFKC`` runs on the two
    presentation-form ranges. Non-Arabic compatibility characters (such as ``²``
    or ``½``) are never touched.
    """
    if not text:
        return ""
    out = text.translate(_TWIN_FORMS) if fold_twins else text
    if not _PRES_RE.search(out):
        return out
    return _PRES_RE.sub(
        lambda m: "".join(unicodedata.normalize("NFKC", ch) for ch in m.group(0)),
        out,
    )


def fold_arabic_variants(text: str) -> str:
    """Fold U+06CC, U+06BE, and U+06A9 to Arabic letters unless Persian/Urdu letters exist."""
    if not text:
        return ""
    if any(ch in _NON_ARABIC_LETTERS for ch in text):
        return text
    if not _TWIN_BASE_RE.search(text):
        return text
    out: List[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        if ch == "\u06cc":
            nxt = text[i + 1] if i + 1 < n else " "
            out.append("\u064a" if _is_rtl_letter(nxt) else "\u0649")
        elif ch == "\u06be":
            out.append("\u0647")
        elif ch == "\u06a9":
            out.append("\u0643")
        else:
            out.append(ch)
    return "".join(out)


def join_split_tokens(line: str, known_ltr: Optional[Set[str]]) -> str:
    """Rejoin Latin/numeric tokens split by layout mode when whole in plain text."""
    if not line or not known_ltr:
        return line
    parts = _SPLIT_SPACES_RE.split(line)
    n = len(parts)
    if n < 3:
        return line

    cls_get = _BIDI_CACHE.get
    has_rtl_part: List[bool] = [False] * n
    in_known_part: List[bool] = [False] * n
    for idx in range(0, n, 2):
        piece = parts[idx]
        if piece:
            in_known_part[idx] = piece in known_ltr
            for ch in piece:
                bc = cls_get(ch)
                if bc is None:
                    bc = _cls(ch)
                if bc in ("R", "AL"):
                    has_rtl_part[idx] = True
                    break

    out: List[str] = []
    i = 0
    while i < n:
        piece = parts[i]
        if (i & 1) or not piece or has_rtl_part[i]:
            out.append(piece)
            i += 1
            continue
        best_j = -1
        best_joined = ""
        j = i
        joined = piece
        all_known = in_known_part[i]
        piece_count = 1
        while j + 2 < n and piece_count < MAX_SPLIT_TOKEN_PIECES:
            nxt_idx = j + 2
            nxt = parts[nxt_idx]
            if not nxt or has_rtl_part[nxt_idx]:
                break
            joined += nxt
            piece_count += 1
            all_known = all_known and in_known_part[nxt_idx]
            j = nxt_idx
            if not all_known and joined in known_ltr:
                best_j = j
                best_joined = joined
        if best_j >= 0:
            out.append(best_joined)
            i = best_j + 1
        else:
            out.append(piece)
            i += 1
    return "".join(out)


def _is_ar_word_char(ch: str) -> bool:
    cp = ord(ch)
    if cp < 0x0621 or cp > 0x08FF:
        return False
    if cp <= 0x065F or (0x066E <= cp <= 0x06D3):
        return True
    if (0x06D5 <= cp <= 0x06DC) or (0x06DF <= cp <= 0x06E8):
        return True
    if (0x06EA <= cp <= 0x06EF) or (0x06FA <= cp <= 0x06FF):
        return True
    return (0x0750 <= cp <= 0x077F) or cp >= 0x08A0


def _repair_ar_run(parts: List[str], known: Set[str], max_known_len: int) -> str:
    m_len = len(parts)
    part_known = [p in known for p in parts]
    out: List[str] = []
    i = 0
    while i < m_len:
        best_k = 0
        best_word = ""
        cand = parts[i]
        all_known = part_known[i]
        limit = min(m_len - i, MAX_WORD_REPAIR_PIECES)
        for k in range(2, limit + 1):
            cand += parts[i + k - 1]
            if len(cand) > max_known_len:
                break
            all_known = all_known and part_known[i + k - 1]
            if not all_known and cand in known:
                best_k = k
                best_word = cand
        if best_k:
            out.append(best_word)
            i += best_k
        else:
            out.append(parts[i])
            i += 1
    return " ".join(out)


def repair_words(line: str, known: Set[str]) -> str:
    """Rejoin Arabic words split by stray layout spaces using page plain-text words.

    Joins at most ``MAX_WORD_REPAIR_PIECES`` (12) pieces and only when the joined
    word appears in ``known`` and the pieces are not all known words themselves.
    Runs in a single linear character scan with no backtracking regular expression.
    """
    if not line or not known or " " not in line:
        return line

    max_known_len = max((len(w) for w in known), default=0)
    if max_known_len < 2:
        return line

    n = len(line)
    out_chunks: List[str] = []
    pos = 0
    changed = False

    while pos < n:
        if not _is_ar_word_char(line[pos]):
            start_non = pos
            pos += 1
            while pos < n and not _is_ar_word_char(line[pos]):
                pos += 1
            out_chunks.append(line[start_non:pos])
            continue

        end0 = pos + 1
        while end0 < n and _is_ar_word_char(line[end0]):
            end0 += 1

        if end0 >= n - 1 or line[end0] != " " or not _is_ar_word_char(line[end0 + 1]):
            out_chunks.append(line[pos:end0])
            pos = end0
            continue

        parts: List[str] = [line[pos:end0]]
        cur = end0
        while cur < n - 1 and line[cur] == " " and _is_ar_word_char(line[cur + 1]):
            w_start = cur + 1
            w_end = w_start + 1
            while w_end < n and _is_ar_word_char(line[w_end]):
                w_end += 1
            parts.append(line[w_start:w_end])
            cur = w_end

        repaired = _repair_ar_run(parts, known, max_known_len)
        orig = line[pos:cur]
        if repaired != orig:
            changed = True
        out_chunks.append(repaired)
        pos = cur

    return "".join(out_chunks) if changed else line


# ---------------------------------------------------------------------------
# 4. Linear RTL and LTR segment reordering
# ---------------------------------------------------------------------------


def _rtl_segment(seg: str, *, word: bool = False) -> str:
    """Convert one visual segment with right-to-left base direction into reading order."""
    if not seg:
        return ""
    rev = seg[::-1]
    n = len(rev)

    cls_get = _BIDI_CACHE.get
    raw_cls: List[str] = []
    for ch in rev:
        bc = cls_get(ch)
        if bc is None:
            bc = _cls(ch)
        raw_cls.append(bc)

    t = list(raw_cls)
    for i in range(n):  # W1: combining mark takes the class of the preceding character
        if t[i] == "NSM":
            t[i] = t[i - 1] if i else "R"

    typ: List[str] = []
    for bc in t:
        if bc in ("R", "AL"):
            typ.append("R")
        elif bc == "L":
            typ.append("L")
        elif bc in ("EN", "AN"):
            typ.append("D")
        else:
            typ.append("N")

    groups: List[Tuple[int, int, str]] = []
    i = 0
    while i < n:
        if typ[i] == "D":
            j = i + 1
            kind = t[i]
            while j < n and typ[j] == "D" and t[j] == kind:
                j += 1
            groups.append((i, j, kind))
            i = j
        else:
            i += 1

    def _tight_sep(a: int, b: int) -> bool:
        span = b - a
        if span <= 0:
            return True
        if span > MAX_TIGHT_SEP_CHARS:
            return False
        if all(raw_cls[k] == "WS" for k in range(a, b)):
            return True
        return all(
            (raw_cls[k] in _TIGHT_CLASSES or rev[k] in _TIGHT_ON_CHARS)
            for k in range(a, b)
        )

    def _tight_L_right(pos: int) -> bool:
        stop = min(n, pos + MAX_TIGHT_SEP_CHARS + 1)
        for k in range(pos, stop):
            if typ[k] != "N":
                return typ[k] == "L" and _tight_sep(pos, k)
        return False

    def _tight_L_left(pos: int) -> bool:
        stop = max(-1, pos - MAX_TIGHT_SEP_CHARS - 2)
        for k in range(pos - 1, stop, -1):
            if typ[k] != "N":
                return typ[k] == "L" and _tight_sep(k + 1, pos)
        return False

    # Two linear passes propagate European digit groups adjacent to Latin islands
    # to the exact fixpoint of the prototype's loop in O(n) time.
    for a, b, kind in groups:
        if kind != "EN" or typ[a] == "L":
            continue
        if _tight_L_left(a) or _tight_L_right(b):
            for k in range(a, b):
                typ[k] = "L"

    for a, b, kind in reversed(groups):
        if kind != "EN" or typ[a] == "L":
            continue
        if _tight_L_right(b) or _tight_L_left(a):
            for k in range(a, b):
                typ[k] = "L"

    # Stand-alone numbers: context comes from the nearest strong char logically before.
    last_strong = "R"
    i = 0
    while i < n:
        cur_typ = typ[i]
        if cur_typ == "R":
            last_strong = "AL" if t[i] == "AL" else "R"
        elif cur_typ == "L":
            last_strong = "L"
        elif cur_typ == "D":
            arabic_ctx = word or (t[i] == "AN") or (last_strong == "AL")
            j = i
            while True:
                while j < n and typ[j] == "D":
                    j += 1
                if j + 1 < n and typ[j + 1] == "D" and typ[j] == "N":
                    sep_cls = t[j]
                    if (sep_cls == "CS" and not (word and rev[j] == "/")) or (
                        sep_cls == "ES" and not arabic_ctx
                    ):
                        j += 1
                        continue
                break
            a, b = i, j
            if not arabic_ctx:
                while a > 0 and t[a - 1] == "ET" and typ[a - 1] == "N":
                    a -= 1
                while b < n and t[b] == "ET" and typ[b] == "N":
                    b += 1
            for k in range(a, b):
                typ[k] = "NUM"
            i = b
            continue
        i += 1

    # Prefix sums give O(1) range queries for L / R / NUM inside bracket spans.
    pref_L = [0] * (n + 1)
    pref_R = [0] * (n + 1)
    pref_NUM = [0] * (n + 1)
    for idx, tp in enumerate(typ):
        pref_L[idx + 1] = pref_L[idx] + (1 if tp == "L" else 0)
        pref_R[idx + 1] = pref_R[idx] + (1 if tp == "R" else 0)
        pref_NUM[idx + 1] = pref_NUM[idx] + (1 if tp == "NUM" else 0)

    mirror = [False] * n
    stack: List[int] = []
    for i in range(n):
        ch = rev[i]
        if typ[i] != "N" or ch not in _PARTNER:
            continue
        if (
            stack
            and rev[stack[-1]] == _PARTNER[ch]
            and (rev[stack[-1]] in _CLOSE_BRACKETS) != (ch in _CLOSE_BRACKETS)
        ):
            a = stack.pop()
            if rev[a] in _CLOSE_BRACKETS:
                has_inner_L = (pref_L[i] - pref_L[a + 1]) > 0
                has_inner_R = (pref_R[i] - pref_R[a + 1]) > 0
                has_inner_NUM = (pref_NUM[i] - pref_NUM[a + 1]) > 0
                joined = False
                if has_inner_L and not has_inner_R and not has_inner_NUM:
                    if _tight_L_right(i + 1):
                        typ[a] = "L"
                        typ[i] = "L"
                        joined = True
                if not joined:
                    mirror[a] = True
                    mirror[i] = True
        else:
            stack.append(i)

    for idx in stack:
        before = rev[idx - 1] if idx > 0 else " "
        after = rev[idx + 1] if idx + 1 < n else " "
        want_close = (not before.isspace()) and after.isspace()
        want_open = before.isspace() and (not after.isspace())
        if (want_close and rev[idx] in _OPEN_BRACKETS) or (
            want_open and rev[idx] in _CLOSE_BRACKETS
        ):
            mirror[idx] = True

    # Neutrals between Latin and Latin join the Latin island (for Word, not across
    # Arabic-script punctuation U+0600..U+06FF).
    i = 0
    while i < n:
        if typ[i] == "N":
            j = i + 1
            has_ar_punct = word and (0x0600 <= ord(rev[i]) <= 0x06FF)
            while j < n and typ[j] == "N":
                if word and not has_ar_punct and (0x0600 <= ord(rev[j]) <= 0x06FF):
                    has_ar_punct = True
                j += 1
            if i > 0 and j < n and typ[i - 1] == "L" and typ[j] == "L" and not has_ar_punct:
                for k in range(i, j):
                    typ[k] = "L"
            i = j
        else:
            i += 1

    out: List[str] = []
    i = 0
    while i < n:
        cur_typ = typ[i]
        if cur_typ in ("L", "NUM"):
            j = i + 1
            while j < n and typ[j] == cur_typ:
                j += 1
            out.append(rev[i:j][::-1])
            i = j
        else:
            ch = rev[i]
            out.append(_PARTNER[ch] if mirror[i] else ch)
            i += 1
    return "".join(out)


def _ltr_segment(seg: str, *, word: bool = False) -> str:
    """Convert right-to-left spans in place inside a left-to-right line."""
    n = len(seg)
    if n == 0:
        return ""
    cls_get = _BIDI_CACHE.get
    classes: List[str] = []
    for ch in seg:
        bc = cls_get(ch)
        if bc is None:
            bc = _cls(ch)
        classes.append(bc)

    out: List[str] = []
    i = 0
    while i < n:
        if classes[i] in ("R", "AL"):
            j = i + 1
            last = i
            while j < n and classes[j] != "L":
                if classes[j] in ("R", "AL", "NSM"):
                    last = j
                j += 1
            out.append(_rtl_segment(seg[i : last + 1], word=word))
            if j > last + 1:
                out.append(seg[last + 1 : j])
            i = j
        else:
            out.append(seg[i])
            i += 1
    return "".join(out)


def _convert_single_line(
    cleaned_line: str,
    *,
    page_rtl: bool,
    word: bool,
    known_ltr: Optional[Set[str]],
) -> str:
    line = join_split_tokens(cleaned_line, known_ltr)
    if word:
        line = _NUM_SEP_R_RE.sub("", _NUM_SEP_L_RE.sub("", line))
    line = _ANY_SPACES_RE.sub(" ", line).strip()
    if not line:
        return ""

    r_cnt, l_cnt = letter_counts(line)
    cls_get = _BIDI_CACHE.get
    has_rtl_cls = r_cnt > 0 or any(
        (cls_get(c) or _cls(c)) in ("R", "AL") for c in line
    )
    has_ltr_cls = l_cnt > 0 or any((cls_get(c) or _cls(c)) == "L" for c in line)
    if not has_rtl_cls and (has_ltr_cls or not page_rtl):
        return line
    if r_cnt >= l_cnt or page_rtl:
        return _rtl_segment(line, word=word)
    return _ltr_segment(line, word=word)


# ---------------------------------------------------------------------------
# 5. Page-level entry point
# ---------------------------------------------------------------------------


def page_reading_order(
    layout_text: Any,
    plain_text: Optional[str] = None,
    *,
    word: bool = False,
) -> Tuple[str, List[str]]:
    """Convert one PDF page's layout-mode text into logical reading order.

    Returns ``(text, notes)``. If the page contains no right-to-left letters,
    ``layout_text`` is returned unchanged with no notes. Never raises on any
    input: on an unexpected internal error, ``layout_text`` is returned
    unconverted with a note.
    """
    layout_str = (
        layout_text
        if isinstance(layout_text, str)
        else ("" if layout_text is None else str(layout_text))
    )
    try:
        if not layout_str or not has_rtl_letters(layout_str):
            return layout_str, []

        plain_str = plain_text if isinstance(plain_text, str) else ""
        norm_layout = layout_str.replace("\r\n", "\n").replace("\r", "\n")
        raw_lines = norm_layout.split("\n")

        page_r = 0
        page_l = 0
        for ln in raw_lines:
            r_c, l_c = letter_counts(ln)
            page_r += r_c
            page_l += l_c
        page_rtl = page_r > 0 and page_r >= page_l

        known_ltr: Set[str] = set()
        if plain_str:
            cls_get = _BIDI_CACHE.get
            for tok in plain_str.split():
                if len(tok) <= MAX_LINE_CHARS and not any(
                    (cls_get(c) or _cls(c)) in ("R", "AL") for c in tok
                ):
                    known_ltr.add(tok)
                    stripped = tok.strip(".,;:!?()[]{}\u060c\u061b\u061f")
                    if stripped:
                        known_ltr.add(stripped)

        converted_lines: List[str] = []
        unconverted_mask: List[bool] = []
        long_line_count = 0

        for raw_ln in raw_lines:
            cleaned = raw_ln.translate(_STRIP_FORMAT_TABLE).replace("\t", " ")
            collapsed_probe = _ANY_SPACES_RE.sub(" ", cleaned).strip(" ")
            if len(collapsed_probe) > MAX_LINE_CHARS:
                converted_lines.append(raw_ln)
                unconverted_mask.append(True)
                long_line_count += 1
                continue
            bounded_spaces = _WIDE_SPACE_CAP_RE.sub("      ", cleaned)
            converted_lines.append(
                _convert_single_line(
                    bounded_spaces,
                    page_rtl=page_rtl,
                    word=bool(word),
                    known_ltr=known_ltr,
                )
            )
            unconverted_mask.append(False)

        # Detect whether any genuine Persian/Urdu-only letter appears on the page.
        has_non_arabic = False
        for idx, ln in enumerate(converted_lines):
            if unconverted_mask[idx] or not ln:
                continue
            probed = normalize_arabic_presentation_forms(ln, fold_twins=False)
            if any(ch in _NON_ARABIC_LETTERS for ch in probed):
                has_non_arabic = True
                break
        fold = not has_non_arabic

        for idx, ln in enumerate(converted_lines):
            if unconverted_mask[idx] or not ln:
                continue
            normed = normalize_arabic_presentation_forms(ln, fold_twins=fold)
            if fold:
                normed = fold_arabic_variants(normed)
            converted_lines[idx] = normed

        if plain_str:
            norm_plain = normalize_arabic_presentation_forms(
                plain_str, fold_twins=fold
            )
            if fold:
                norm_plain = fold_arabic_variants(norm_plain)
            known_words = set(_WORD_RE.findall(norm_plain))
            if known_words:
                for idx, ln in enumerate(converted_lines):
                    if not unconverted_mask[idx] and ln:
                        converted_lines[idx] = repair_words(ln, known_words)

        notes: List[str] = []
        if long_line_count:
            notes.append(
                f"{long_line_count} line(s) exceeded {MAX_LINE_CHARS} characters "
                "after collapsing spaces and were left unconverted."
            )
        return "\n".join(converted_lines), notes
    except MemoryError:
        raise
    except Exception as exc:
        return (
            layout_str,
            [
                f"Reading-order conversion failed ({type(exc).__name__}); "
                "returned layout text unconverted."
            ],
        )
