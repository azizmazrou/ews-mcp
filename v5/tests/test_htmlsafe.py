"""Tests for ewsmcp.htmlsafe: custom linear-time tokenizer, independent lxml.html
tree & allow-list checker, attack corpus, 5,000+ seeded fuzz cases, deceptive link
matrix, stored-HTML link extractor differentials, RTL & wrapper preservation, and
200,000-character linear-time cost bounds.
"""

from __future__ import annotations

import html
import random
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import lxml.html
import pytest

import ewsmcp.htmlsafe as htmlsafe_mod
from ewsmcp.htmlsafe import (
    MAX_HTML_CHARS,
    MAX_INSPECT_HTML_CHARS,
    MAX_MISMATCH_WARNINGS,
    MAX_PREVIEW_HOSTS,
    MAX_PREVIEW_LINK_TEXT_CHARS,
    MAX_PREVIEW_LINKS,
    HTMLValidationError,
    inspect_draft_links,
    sanitise_html,
)

# ---------------------------------------------------------------------------
# Independent specification allow-lists & two-layer verifier (strict output
# grammar + libxml2 / lxml.html tree agreement; never imports htmlsafe tables
# or html.parser).
# ---------------------------------------------------------------------------

from conftest import TIME_MARGIN  # noqa: E402


SPEC_ELEMENTS = frozenset({
    "p", "br", "div", "span",
    "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "colgroup", "col",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code", "hr",
    "a", "font",
})
SPEC_VOID = frozenset({"br", "hr", "col"})
# Inline formatting elements. HTML5 keeps a block that is written inside one of these
# (<b><p>x</p></b> stays nested) while the libxml2 parser behind lxml closes the inline
# element first. Both trees hold the same elements in the same order, so the fuzz test
# compares the nearest ancestor that is NOT one of these; the hand-written corpus still
# compares direct parents.
SPEC_INLINE = frozenset({
    "a", "b", "strong", "i", "em", "u", "s", "strike", "sub", "sup", "span", "font", "code",
})
SPEC_GLOBAL_ATTRS = frozenset({"dir", "lang", "align", "style"})
SPEC_TAG_ATTRS = {
    "a": frozenset({"href", "title"}),
    "td": frozenset({"colspan", "rowspan", "width", "height"}),
    "th": frozenset({"colspan", "rowspan", "width", "height"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "width"}),
    "font": frozenset({"face", "size", "color"}),
    "ol": frozenset({"start", "type"}),
    "col": frozenset({"span", "width"}),
}
SPEC_SCHEMES = frozenset({"http", "https", "mailto"})
SPEC_STYLE_PROPS = frozenset({
    "direction", "unicode-bidi", "text-align", "vertical-align",
    "line-height", "white-space", "font-family", "font-size",
    "font-weight", "font-style", "text-decoration", "color",
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
    "width", "height", "list-style-type",
})
SPEC_STYLE_PUNCT = frozenset(" #%,.()'\"-+!")

_WRAPPER_RE = re.compile(r"^<html><body((?: [a-z]+=\"[^\"<>]*\")*)>(.*)</body></html>$", re.DOTALL)
_TOKEN_RE = re.compile(r'<(/?)([a-z][a-z0-9]*)((?: [a-z]+="[^"<>]*")*)>|([^<>]+)')
_ATTR_RE = re.compile(r' ([a-z]+)="([^"<>]*)"')
_TEXT_AMP_RE = re.compile(r"&(?!amp;|lt;|gt;)")
_ATTR_AMP_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;)")
_LXML_PARSER = lxml.html.HTMLParser(
    huge_tree=True,
    recover=True,
    remove_comments=False,
    remove_pis=False,
    encoding="utf-8",
)


def _check_spec_style(val: str) -> None:
    low = val.casefold()
    compact = "".join(low.split())
    for bad in ("url(", "expression", "javascript", "vbscript", "behavior", "-moz-binding"):
        assert bad not in low and bad not in compact, f"style contains {bad!r}: {val!r}"
    for bad_ch in ("@", "\\", "<", ">", "/*"):
        assert bad_ch not in val, f"style contains {bad_ch!r}: {val!r}"
    decls = [d.strip() for d in val.split(";") if d.strip()]
    assert decls, f"empty style: {val!r}"
    for decl in decls:
        assert ":" in decl, f"style declaration without colon: {decl!r}"
        prop, _, css_val = decl.partition(":")
        prop, css_val = prop.strip(), css_val.strip()
        assert prop in SPEC_STYLE_PROPS, f"disallowed style prop {prop!r}"
        assert 1 <= len(css_val) <= 200, f"bad style length: {css_val!r}"
        for ch in css_val:
            assert ch.isalpha() or ch.isdecimal() or ch in SPEC_STYLE_PUNCT, (
                f"disallowed char {ch!r} in style {css_val!r}"
            )
        if prop == "unicode-bidi":
            assert css_val in ("normal", "embed", "isolate", "plaintext"), css_val
        quote: str | None = None
        depth = 0
        for ch in css_val:
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                assert depth >= 0, f"unbalanced ')' in {css_val!r}"
        assert quote is None and depth == 0, f"unbalanced quotes/parens in {css_val!r}"


def _check_spec_href(val: str) -> None:
    decoded = html.unescape(html.unescape(val))
    compact = "".join(
        ch for ch in decoded
        if not ch.isspace() and unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )
    assert ":" in compact, f"href without scheme: {val!r}"
    scheme = compact.split(":", 1)[0].lower()
    assert scheme in SPEC_SCHEMES, f"disallowed scheme {scheme!r} in {val!r}"
    for ch in val:
        code = ord(ch)
        assert code >= 0x20 and code != 0x7F and ch not in "\t\n\r", f"ctrl in href: {val!r}"
        assert unicodedata.category(ch) not in ("Cc", "Cf", "Cs"), f"cf in href: {val!r}"
    parts = urlsplit(val)
    assert parts.scheme.lower() in SPEC_SCHEMES, f"bad urlsplit scheme: {val!r}"
    if parts.scheme.lower() in ("http", "https"):
        assert re.match(r"(?i)^https?://", val), f"missing // in {val!r}"
        assert parts.hostname, f"missing hostname in {val!r}"
        assert "@" not in parts.netloc, f"userinfo in {val!r}"
    else:
        assert "?" not in val and "#" not in val, f"mailto query/fragment survived: {val!r}"
        assert "," not in parts.path and ";" not in parts.path, f"multi-addr mailto: {val!r}"


def _check_spec_attr(tag: str, name: str, val: str) -> None:
    assert not name.startswith("on"), f"event handler {name!r} survived"
    allowed = SPEC_GLOBAL_ATTRS if tag == "body" else (
        SPEC_GLOBAL_ATTRS | SPEC_TAG_ATTRS.get(tag, frozenset())
    )
    assert name in allowed, f"disallowed attr {name!r} on <{tag}>"
    for ch in val:
        code = ord(ch)
        assert not ((code < 0x20 and ch not in "\t\n\r") or code == 0x7F), val
        assert not (0xD800 <= code <= 0xDFFF), val
        assert code not in (0x202D, 0x202E), f"bidi override in attr {name}={val!r}"
    if name == "href":
        _check_spec_href(val)
    elif name == "style":
        _check_spec_style(val)
    elif name == "dir":
        assert val in ("ltr", "rtl", "auto"), val
    elif name == "align":
        assert val in ("left", "right", "center", "justify"), val
    elif name == "lang":
        assert re.fullmatch(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*", val), val
    elif name in ("colspan", "rowspan", "span", "border", "cellpadding", "cellspacing"):
        assert re.fullmatch(r"\d{1,4}", val), val
    elif name in ("width", "height"):
        assert re.fullmatch(r"\d{1,5}(\.\d{1,2})?(%|px)?", val), val
    elif name == "size":
        assert re.fullmatch(r"[+-]?[1-7]", val), val
    elif name == "color":
        assert re.fullmatch(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?|[A-Za-z]{2,30}", val), val
    elif name == "start":
        assert re.fullmatch(r"-?\d{1,6}", val), val
    elif name == "type":
        assert val in ("1", "a", "A", "i", "I"), val
    elif name == "face":
        _check_spec_style("font-family: " + val)
    elif name == "title":
        assert len(val) <= 1000


def _parse_strict_attr_blob(tag: str, attr_blob: str) -> list[tuple[str, str]]:
    attrs: list[tuple[str, str]] = []
    seen: set[str] = set()
    consumed = 0
    for am in _ATTR_RE.finditer(attr_blob):
        consumed += am.end() - am.start()
        aname, araw = am.group(1), am.group(2)
        assert not _ATTR_AMP_RE.search(araw), f"bad & escape in attr: {araw!r}"
        aval = html.unescape(araw)
        assert aname not in seen, f"duplicate attribute {aname!r}"
        seen.add(aname)
        _check_spec_attr(tag, aname, aval)
        attrs.append((aname, aval))
    assert consumed == len(attr_blob), f"malformed attribute blob on <{tag}>: {attr_blob!r}"
    return attrs


def strict_token_summary(doc: str) -> dict[str, Any]:
    assert "\x00" not in doc, "NUL byte in output"
    m_wrap = _WRAPPER_RE.match(doc)
    assert m_wrap is not None, f"missing <html><body...> wrapper: {doc[:80]!r}"
    body_attr_blob, inner = m_wrap.group(1), m_wrap.group(2)
    _parse_strict_attr_blob("body", body_attr_blob)

    stack: list[str] = ["body"]
    tags: list[str] = []
    parents: list[str] = []
    block_parents: list[str] = []
    text_parts: list[str] = []
    links: list[tuple[str, str]] = []
    cur_href: str | None = None
    link_buf: list[str] = []

    pos = 0
    n = len(inner)
    while pos < n:
        tm = _TOKEN_RE.match(inner, pos)
        assert tm is not None, f"output grammar break at {pos}: {inner[pos:pos + 60]!r}"
        pos = tm.end()
        if tm.group(4) is not None:
            raw_t = tm.group(4)
            assert not _TEXT_AMP_RE.search(raw_t), f"unexpected & in text: {raw_t[:60]!r}"
            decoded_t = html.unescape(raw_t)
            for bad_c in ("\u202d", "\u202e", "\ufffe", "\uffff"):
                assert bad_c not in decoded_t, f"forbidden char {ord(bad_c):#x} in text"
            text_parts.append(decoded_t)
            if cur_href is not None:
                link_buf.append(decoded_t)
            continue

        closing, name, attr_blob = tm.group(1), tm.group(2), tm.group(3)
        assert name in SPEC_ELEMENTS, f"element <{closing}{name}> not in allow-list"
        if closing:
            assert not attr_blob, f"attributes on closing </{name}>"
            assert name not in SPEC_VOID, f"closing tag for void <{name}>"
            assert len(stack) > 1 and stack[-1] == name, (
                f"misnested </{name}> with stack {stack[-4:]!r}"
            )
            stack.pop()
            if name == "a" and cur_href is not None:
                links.append((cur_href, " ".join("".join(link_buf).split())))
                cur_href = None
            continue

        attrs = _parse_strict_attr_blob(name, attr_blob)
        tags.append(name)
        parents.append(stack[-1])
        block_parents.append(next(t for t in reversed(stack) if t not in SPEC_INLINE))
        if name == "a":
            cur_href = dict(attrs).get("href")
            link_buf = []
        if name not in SPEC_VOID:
            stack.append(name)

    assert stack == ["body"], f"unclosed elements at EOF: {stack!r}"
    return {
        "tags": tags,
        "parents": parents,
        "block_parents": block_parents,
        "text": "".join(text_parts),
        "links": links,
    }


def lxml_tree_summary(doc: str) -> dict[str, Any]:
    root = lxml.html.document_fromstring(doc.encode("utf-8"), parser=_LXML_PARSER)
    tags: list[str] = []
    parents: list[str] = []
    block_parents: list[str] = []
    links: list[tuple[str, str]] = []

    for el in root.iter():
        assert isinstance(el.tag, str), f"non-element node in lxml parse: {el!r}"
        tag = el.tag.lower()
        if tag == "html":
            assert not el.attrib, f"<html> must have no attributes: {dict(el.attrib)}"
            continue
        if tag == "body":
            for aname, aval in el.attrib.items():
                _check_spec_attr("body", aname.lower(), aval)
            continue
        assert tag in SPEC_ELEMENTS, f"disallowed element in lxml tree: <{tag}>"
        tags.append(tag)
        par = el.getparent()
        parents.append(par.tag.lower() if par is not None and isinstance(par.tag, str) else "#")
        anc = par
        while anc is not None and isinstance(anc.tag, str) and anc.tag.lower() in SPEC_INLINE:
            anc = anc.getparent()
        block_parents.append(anc.tag.lower() if anc is not None and isinstance(anc.tag, str) else "#")
        for aname, aval in el.attrib.items():
            _check_spec_attr(tag, aname.lower(), aval)
        if tag == "a" and "href" in el.attrib:
            links.append((el.attrib["href"], " ".join(el.text_content().split())))

    return {
        "tags": tags,
        "parents": parents,
        "block_parents": block_parents,
        "text": root.text_content(),
        "links": links,
    }


def assert_allowlist_valid(doc: str) -> None:
    tsum = strict_token_summary(doc)
    lx = lxml_tree_summary(doc)
    assert lx["tags"] == tsum["tags"], (
        f"lxml vs builder tag sequence drift: lxml={lx['tags'][:30]} "
        f"builder={tsum['tags'][:30]}"
    )
    assert lx["block_parents"] == tsum["block_parents"], (
        f"lxml vs builder block-parent drift: "
        f"lxml={list(zip(lx['tags'], lx['block_parents']))[:25]} "
        f"builder={list(zip(tsum['tags'], tsum['block_parents']))[:25]}"
    )
    for tag, lpar, tpar in zip(tsum["tags"], lx["parents"], tsum["parents"]):
        if not (tpar in SPEC_INLINE and tag not in SPEC_INLINE):
            assert lpar == tpar, (
                f"lxml vs builder parent tree drift on <{tag}>: "
                f"lxml={lpar!r} builder={tpar!r}"
            )
    assert "".join(lx["text"].split()) == "".join(tsum["text"].split())


# ---------------------------------------------------------------------------
# 1. Attack & structural corpus (>= 115 cases asserted on exact output,
#    independent allow-list + lxml.html tree agreement, and idempotence)
# ---------------------------------------------------------------------------

DEEP_1500_IN = "<div>" * 1500 + "deep" + "</div>" * 1500
DEEP_1500_OUT = "<html><body>" + "<div>" * 1500 + "deep" + "</div>" * 1500 + "</body></html>"
DEEP_SCRIPT_IN = "<span>" * 1100 + "<script>alert(1)</script>ok" + "</span>" * 1100
DEEP_SCRIPT_OUT = "<html><body>" + "<span>" * 1100 + "ok" + "</span>" * 1100 + "</body></html>"

ATTACK_CORPUS: list[tuple[str, str, str]] = [
    # --- Script & style blocks ---
    (
        "script_basic",
        "<script>alert(1)</script><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "script_uppercase_src",
        '<SCRIPT SRC="https://example.com/x.js">evil()</SCRIPT><p>ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "script_lt_inside",
        "<script>if (1 < 2 && 3 > 1) { alert('x'); }</script><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "script_unclosed_after_text",
        "<p>ok</p><script>alert(document.cookie)",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "script_self_closing_slash_quirk",
        "<script/>alert(1)</script><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "script_end_tag_with_attr_or_slash",
        "<script>bad()</script x><script>bad2()</script/><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "style_block_basic",
        "<style>body { background: url(https://example.com/bg); }</style><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "style_self_closing_slash_quirk",
        "<style/>p { display: none; }</style><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "style_unclosed_after_text",
        "<p>ok</p><style>@import url(https://example.com/x.css);",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "head_title_link_meta_bundle",
        (
            "<html><head><title>Secret</title>"
            '<link rel="stylesheet" href="https://example.com/a.css">'
            '<meta charset="utf-8"></head><body><p>ok</p></body></html>'
        ),
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "head_omitted_closing_tag_before_body",
        '<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
        "<title>t</title><body><p>ok</p></body></html>",
        '<html><body lang="ar" dir="rtl"><p>ok</p></body></html>',
    ),
    (
        "head_omitted_closing_tag_before_flow_element",
        "<head><title>t</title><style>p{margin:0}</style><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "noscript_and_template_dropped",
        "<noscript><img src=x onerror=alert(1)>hidden</noscript>"
        "<template><script>alert(1)</script>tmpl</template><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    # --- Event handlers (on...) ---
    (
        "handler_onclick_double_quoted",
        '<p onclick="alert(1)">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "handler_onload_on_body_wrapper",
        '<body onload="alert(1)"><p>ok</p></body>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "handler_onmouseover_mixed_case",
        '<span OnMouseOver="alert(1)" ONFOCUS="evil()">ok</span>',
        "<html><body><span>ok</span></body></html>",
    ),
    (
        "handler_onerror_onanimationstart_ontoggle",
        '<div onerror="a()" onanimationstart="b()" ontoggle="c()">ok</div>',
        "<html><body><div>ok</div></body></html>",
    ),
    (
        "handler_on_anchor_with_valid_href",
        '<a href="https://example.com" onclick="steal()" onauxclick="steal()">ok</a>',
        '<html><body><a href="https://example.com">ok</a></body></html>',
    ),
    (
        "handler_pointer_touch_drag_clipboard",
        (
            '<p onpointerdown="x()" ontouchstart="x()" '
            'ondragstart="x()" oncopy="x()" onpaste="x()">ok</p>'
        ),
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "handler_unquoted_and_valueless",
        "<p onclick=alert(1) onmouseover>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    # --- Dangerous & malformed link targets ---
    (
        "href_javascript_plain",
        '<a href="javascript:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_mixed_case",
        '<a href="JaVaScRiPt:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_decimal_entities",
        (
            '<a href="&#106;&#97;&#118;&#97;&#115;&#99;&#114;&#105;&#112;&#116;'
            '&#58;alert(1)">click</a>'
        ),
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_hex_entities",
        (
            '<a href="&#x6A;&#x61;&#x76;&#x61;&#x73;&#x63;&#x72;&#x69;&#x70;&#x74;'
            '&#x3A;alert(1)">click</a>'
        ),
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_padded_no_semicolon_entities",
        (
            '<a href="&#0000106&#0000097&#0000118&#0000097&#0000115&#0000099'
            '&#0000114&#0000105&#0000112&#0000116&#0000058alert(1)">click</a>'
        ),
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_double_encoded",
        '<a href="&amp;#106;avascript:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_tab_split",
        '<a href="java\tscript:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_newline_entity_split",
        '<a href="jav&#x0A;ascript&#x09;:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_javascript_leading_control_char",
        '<a href="&#14;  javascript:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_vbscript_plain",
        '<a href="vbscript:msgbox(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_vbscript_mixed_entity_and_ws",
        '<a href=" &#x76;b\r\nSCript&#58;Execute(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_data_html_plain",
        '<a href="data:text/html,<script>alert(1)</script>">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_data_base64_mixed_case",
        '<a href="DaTa:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_data_entity_ws_split",
        '<a href="d&#x61;\tta&#x3a;image/svg+xml,<svg onload=alert(1)>">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_file_unix",
        '<a href="file:///etc/passwd">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_file_mixed_entity_control",
        '<a href="\x07Fi&#x6c;e:///c:/boot.ini">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_protocol_relative_refused",
        '<a href="//example.com/path">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_relative_path_refused",
        '<a href="/internal/admin">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_http_missing_slashes_refused",
        '<a href="https:javascript:alert(1)">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_http_userinfo_spoof_refused",
        '<a href="https://portal.example.com@other.example.com/login">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_http_backslash_authority_refused",
        '<a href="https://other.example.com\\@portal.example.com">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_bidi_override_in_url_refused",
        '<a href="https://example.com/\u202emco.elpmaxe">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_unclosed_ipv6_bracket_removed_not_raised",
        '<p><a href="https://[">x</a> hello</p>',
        "<html><body><p><a>x</a> hello</p></body></html>",
    ),
    (
        "href_non_ip_bracket_host_removed_not_raised",
        '<p><a href="https://[example.com]/">x</a> hello</p>',
        "<html><body><p><a>x</a> hello</p></body></html>",
    ),
    (
        "href_fullwidth_slash_in_host_removed_not_raised",
        '<p><a href="https://example.com\uff0fx">x</a> hello</p>',
        "<html><body><p><a>x</a> hello</p></body></html>",
    ),
    (
        "href_percent_dot_and_ideographic_dot_in_host_removed",
        '<p><a href="https://portal.example.com%2eevil.example/">a</a> '
        '<a href="https://portal.example.com\u3002evil.example/">b</a></p>',
        "<html><body><p><a>a</a> <a>b</a></p></body></html>",
    ),
    (
        "href_mailto_empty_refused",
        '<a href="mailto:   ">click</a>',
        "<html><body><a>click</a></body></html>",
    ),
    (
        "href_mailto_query_and_extra_recipients_stripped",
        '<a href="mailto:team@example.com,spy@evil.example?bcc=leak@evil.example&amp;body=hi">'
        "team@example.com</a>",
        '<html><body><a href="mailto:team@example.com">team@example.com</a></body></html>',
    ),
    (
        "href_https_uppercase_and_unterminated_query_entities_preserved",
        '<a href="HTTPS://Example.com/search?lang=ar&region=sa&copy=1&not=2&amp;ok=3">docs</a>',
        '<html><body><a href="https://Example.com/search?'
        'lang=ar&amp;region=sa&amp;copy=1&amp;not=2&amp;ok=3">docs</a></body></html>',
    ),
    # --- CSS style attacks & rules ---
    (
        "css_url_in_background_image",
        '<p style="background-image: url(https://example.com/track.png)">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_url_in_allowed_prop_color",
        '<p style="background-color: URL(https://example.com/track.png)">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_url_with_whitespace_before_paren",
        '<p style="background-color: url (https://example.com/track.png)">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_expression_ie",
        '<div style="width: expression(alert(1))">ok</div>',
        "<html><body><div>ok</div></body></html>",
    ),
    (
        "css_expression_mixed_case_kept_neighbor",
        '<div style="color: #123456; width: ExPrEsSiOn(alert(1)); text-align: right">ok</div>',
        '<html><body><div style="color: #123456; text-align: right">ok</div></body></html>',
    ),
    (
        "css_at_import",
        "<p style=\"@import 'https://example.com/evil.css'; color: red\">ok</p>",
        '<html><body><p style="color: red">ok</p></body></html>',
    ),
    (
        "css_at_symbol_in_value",
        '<p style="font-family: @Arial">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_backslash_hex_escape",
        r'<p style="background-color: \75\72\6c(x)">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_comment_inside_value",
        '<p style="color: /* evil */ red">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_comment_in_property_name",
        '<p style="/* evil */ color: red">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_javascript_token_in_value",
        '<p style="font-family: javascript">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_angle_brackets_in_value",
        "<p style=\"font-family: '<script>'\">ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_disallowed_overlay_and_hidden_props",
        (
            '<div style="position: fixed; top: 0; left: 0; z-index: 9999; '
            'display: none; visibility: hidden; opacity: 0; color: navy">ok</div>'
        ),
        '<html><body><div style="color: navy">ok</div></body></html>',
    ),
    (
        "css_value_over_200_chars_refused",
        f'<p style="font-family: {"a" * 201}; color: green">ok</p>',
        '<html><body><p style="color: green">ok</p></body></html>',
    ),
    (
        "css_unbalanced_quotes_and_parens_refused",
        "<p style=\"font-family: 'unclosed; color: rgb(1, 2\">ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "css_multiline_whitespace_normalised",
        "<p style=\"font-family: 'Traditional Arabic',\n  Tahoma,\t sans-serif; "
        'font-size: 14pt">ok</p>',
        "<html><body><p style=\"font-family: 'Traditional Arabic', Tahoma, sans-serif; "
        'font-size: 14pt">ok</p></body></html>',
    ),
    (
        "css_unicode_bidi_override_dropped_isolate_kept",
        '<p style="unicode-bidi: bidi-override; direction: rtl">a</p>'
        '<p style="unicode-bidi: isolate-override">b</p>'
        '<p style="unicode-bidi: embed; direction: rtl">c</p>',
        '<html><body><p style="direction: rtl">a</p><p>b</p>'
        '<p style="unicode-bidi: embed; direction: rtl">c</p></body></html>',
    ),
    (
        "css_tiny_and_zero_font_size_dropped",
        '<p style="font-size: 0">a</p><p style="font-size: 5px">b</p>'
        '<p style="font-size: 5.9pt">c</p><p style="font-size: 6px">d</p>',
        '<html><body><p>a</p><p>b</p><p>c</p><p style="font-size: 6px">d</p></body></html>',
    ),
    # --- Dropped elements ---
    (
        "drop_img_remote_tracking_pixel",
        '<p>Hello<img src="https://example.com/open?id=42" alt="secret">World</p>',
        "<html><body><p>HelloWorld</p></body></html>",
    ),
    (
        "drop_svg_with_nested_script_and_text",
        '<svg onload="alert(1)"><g><text>svg-text</text><script>x()</script></g></svg><p>ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_svg_self_closing_preserves_following_text",
        "<svg/><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_math_block_and_self_closing",
        "<math><mi>x</mi><annotation-xml><script>bad()</script></annotation-xml></math>"
        "<math/><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_iframe_and_slash_iframe",
        '<iframe src="https://example.com/frame">inside</iframe>'
        "<iframe/>also-inside</iframe><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_object_and_embed",
        '<object data="https://example.com/a.swf"><p>fallback</p></object>'
        '<embed src="https://example.com/b.swf"><p>ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_form_inputs_buttons_select_textarea",
        (
            '<form action="https://example.com/collect">'
            '<input type="password" name="pw">'
            "<select><option>One</option><option>Two</select>"
            "<textarea>hidden</textarea>"
            "<button>Send</button>"
            "</form><p>ok</p>"
        ),
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_meta_refresh",
        '<meta http-equiv="refresh" content="0;url=https://example.com"><p>ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_base_href_hijack",
        '<base href="https://example.org/"><a href="https://example.com/x">ok</a>',
        '<html><body><a href="https://example.com/x">ok</a></body></html>',
    ),
    (
        "drop_frameset_frame_applet",
        '<frameset><frame src="https://example.com"></frameset>'
        "<applet>legacy</applet><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "drop_media_canvas_map_area",
        (
            '<audio controls><source src="a.mp3"><track src="t.vtt">fallback</audio>'
            '<video src="v.mp4">vfallback</video>'
            "<canvas>cfallback</canvas>"
            '<map name="m"><area href="javascript:alert(1)"></map><p>ok</p>'
        ),
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "unknown_elements_unwrapped_keeping_text",
        "<section><article><custom-tag data-x='1'>Kept <b>bold</b></custom-tag></article></section>",
        "<html><body>Kept <b>bold</b></body></html>",
    ),
    # --- Comments, conditional comments, marked sections, CDATA, PI, doctype ---
    (
        "comment_standard_and_multiline",
        "<!-- <script>alert(1)</script>\nmultiline --><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "comment_abrupt_and_bang_closers",
        "<!--><!---><!-- x --!><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "comment_unterminated_at_eof_dropped",
        "<p>ok</p><!-- unterminated <script>alert(1)</script>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "comment_conditional_downlevel_hidden",
        "<!--[if IE]><script>alert(1)</script><img src=x><![endif]--><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "comment_conditional_downlevel_revealed",
        "<!--[if gte IE 4]><!--><script>alert(1)</script><!--<![endif]--><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "comment_conditional_bogus_decl",
        "<![if !IE]><script>alert(1)</script><![endif]><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "marked_sections_bogus_never_assert",
        "<p>hello</p><![x]><![CDAT[x]]><![ endif]>",
        "<html><body><p>hello</p></body></html>",
    ),
    (
        "cdata_section_dropped",
        "<![CDATA[<script>alert(1)</script>secret]]><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "processing_instructions_and_unterminated_pi_dropped",
        '<!DOCTYPE html><?xml version="1.0"?><?php echo 1; ?><p>ok</p><?unclosed',
        "<html><body><p>ok</p></body></html>",
    ),
    # --- Malformed, unclosed, implied end tags, and table structure ---
    (
        "malformed_lt_not_followed_by_tag_name_kept_as_text",
        "<p>1 < 2 and a <3 b <> c </p>",
        "<html><body><p>1 &lt; 2 and a &lt;3 b &lt;&gt; c </p></body></html>",
    ),
    (
        "malformed_misnested_b_i",
        "<b><i>bold-italic</b> trailing-italic</i>",
        "<html><body><b><i>bold-italic</i></b> trailing-italic</body></html>",
    ),
    (
        "malformed_unclosed_tags_closed_at_eof",
        "<div><table><tr><td>cell-text",
        "<html><body><div><table><tr><td>cell-text</td></tr></table></div></body></html>",
    ),
    (
        "malformed_adjacent_unclosed_paragraphs_and_list_items",
        "<p>First<p>Second<ul><li>Item 1<li>Item 2</ul>",
        "<html><body><p>First</p><p>Second</p><ul><li>Item 1</li><li>Item 2</li></ul></body></html>",
    ),
    (
        "implied_end_p_before_block_elements",
        "<p>a<div>b</div><p>c<table><tr><td>d<td>e<tr><td>f</table><p>g<h2>h</h2><p>i<hr><p>j",
        "<html><body><p>a</p><div>b</div><p>c</p>"
        "<table><tr><td>d</td><td>e</td></tr><tr><td>f</td></tr></table>"
        "<p>g</p><h2>h</h2><p>i</p><hr><p>j</p></body></html>",
    ),
    (
        "implied_end_table_sections_and_stray_table_tags_dropped",
        "<tr></tr><table><thead><tr><th>H<tbody><tr><td>A<tfoot><tr><td>F</table><td>stray</td>",
        "<html><body><table><thead><tr><th>H</th></tr></thead>"
        "<tbody><tr><td>A</td></tr></tbody><tfoot><tr><td>F</td></tr></tfoot></table>"
        "stray</body></html>",
    ),
    (
        "table_bare_flow_wrapped_in_implicit_row_and_cell",
        "<table>bare<b>bold</b><tr><td>cell</td></tr></table>",
        "<html><body><table><tr><td>bare<b>bold</b></td></tr>"
        "<tr><td>cell</td></tr></table></body></html>",
    ),
    (
        "malformed_nested_anchors_flattened",
        '<a href="https://a.example.com">outer <a href="https://b.example.com">inner</a></a>',
        (
            '<html><body><a href="https://a.example.com">outer </a>'
            '<a href="https://b.example.com">inner</a></body></html>'
        ),
    ),
    (
        "malformed_nested_anchors_across_table_flattened",
        '<a href="https://a.example.com"><table><tr><td>'
        '<a href="https://b.example.com">inner</a></td></tr></table></a>',
        "<html><body><table><tr><td>"
        '<a href="https://b.example.com">inner</a></td></tr></table></body></html>',
    ),
    (
        "implied_end_p_and_pre_before_table",
        "<p>a<pre>\n><table><td>d</table>",
        "<html><body><p>a</p><pre>\n&gt;</pre>"
        "<table><tr><td>d</td></tr></table></body></html>",
    ),
    (
        "malformed_stray_closing_tags_ignored",
        "</div></table></script></body></html><p>ok</p></h1>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "malformed_incomplete_start_tag_at_eof_dropped",
        "<p>ok</p><img src=x onerror=alert(1)",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "malformed_incomplete_end_tag_at_eof_dropped",
        "<p>ok</p></div attr='unclosed",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "malformed_self_closing_nonvoid_div_slash_ignored",
        "<div/>ok",
        "<html><body><div>ok</div></body></html>",
    ),
    # --- Wrapper attribute carry-over ---
    (
        "wrapper_html_and_body_attrs_carried_body_wins",
        '<html dir="ltr" lang="ar" data-x="1">'
        '<body dir="rtl" style="text-align: right; position: fixed"><p>ok</p></body></html>',
        '<html><body dir="rtl" lang="ar" style="text-align: right"><p>ok</p></body></html>',
    ),
    # --- Unquoted, backtick-quoted, and duplicated attributes ---
    (
        "attr_unquoted_valid_and_invalid",
        "<p dir=rtl align=invalid id=main>ok</p>",
        '<html><body><p dir="rtl">ok</p></body></html>',
    ),
    (
        "attr_backtick_href_javascript",
        "<a href=`javascript:alert(1)`>ok</a>",
        "<html><body><a>ok</a></body></html>",
    ),
    (
        "attr_backtick_href_https_refused_as_bad_scheme",
        "<a href=`https://example.com`>ok</a>",
        "<html><body><a>ok</a></body></html>",
    ),
    (
        "attr_backtick_on_title_safely_double_quoted",
        '<a href="https://example.com" title=`tip`>ok</a>',
        '<html><body><a href="https://example.com" title="`tip`">ok</a></body></html>',
    ),
    (
        "attr_duplicate_href_bad_first_wins_and_drops",
        '<a href="javascript:alert(1)" href="https://example.com">ok</a>',
        "<html><body><a>ok</a></body></html>",
    ),
    (
        "attr_duplicate_href_good_first_wins",
        '<a href="https://example.com" href="javascript:alert(1)">ok</a>',
        '<html><body><a href="https://example.com">ok</a></body></html>',
    ),
    (
        "attr_duplicate_dir_case_insensitive_first_wins",
        '<p dir="rtl" DIR="ltr">ok</p>',
        '<html><body><p dir="rtl">ok</p></body></html>',
    ),
    # --- Null bytes & bidi overrides in text ---
    (
        "null_inside_script_tag_name",
        "<scr\x00ipt>alert(1)</scr\x00ipt><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "null_after_opening_bracket",
        "<\x00script>alert(1)</script><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "null_inside_event_handler_name",
        '<p on\x00click="alert(1)">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "null_inside_javascript_scheme",
        '<a href="java\x00script:alert(1)">ok</a>',
        "<html><body><a>ok</a></body></html>",
    ),
    (
        "null_inside_css_expression",
        '<p style="width: expr\x00ession(alert(1))">ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "null_c0_controls_and_bidi_overrides_stripped_from_text",
        "<p>clean\x00\x01\x07\u202d\u202etext</p>",
        "<html><body><p>cleantext</p></body></html>",
    ),
    # --- Mutation patterns ---
    (
        "mutation_markup_in_dropped_img_alt",
        '<img alt="<script>alert(1)</script>" src="x"><p>ok</p>',
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "mutation_quote_breakout_in_dropped_input",
        "<form><input value='\"><script>alert(1)</script>'></form><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "mutation_markup_in_disallowed_attr_on_kept_div",
        '<div data-html="<img src=x onerror=alert(1)>">ok</div>',
        "<html><body><div>ok</div></body></html>",
    ),
    (
        "mutation_markup_in_kept_anchor_title_escaped",
        '<a href="https://example.com" title="</a><script>alert(1)</script>">ok</a>',
        (
            '<html><body><a href="https://example.com" '
            'title="&lt;/a&gt;&lt;script&gt;alert(1)&lt;/script&gt;">ok</a></body></html>'
        ),
    ),
    (
        "mutation_svg_style_comment_closed_inside_svg",
        "<svg><style><!--</style><img src=x onerror=alert(1)>--></svg><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "mutation_math_mglyph_style_payload",
        (
            '<math><mtext><table><mglyph><style><!--</style>'
            '<img title="--><img src=1 onerror=alert(1)>"></math><p>ok</p>'
        ),
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "mutation_title_with_fake_inner_form_tag",
        "<title><form>unclosed-inner</title><p>ok</p>",
        "<html><body><p>ok</p></body></html>",
    ),
    (
        "mutation_xmp_rawtext_unwrapped_and_escaped",
        "<xmp><img src=x onerror=alert(1)>literal</xmp>",
        "<html><body>&lt;img src=x onerror=alert(1)&gt;literal</body></html>",
    ),
    (
        "mutation_escaped_script_stays_escaped_text",
        "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>",
        "<html><body><p>&lt;script&gt;alert(1)&lt;/script&gt;</p></body></html>",
    ),
    # --- Very deep nesting (above Python's recursion limit) ---
    (
        "deep_nesting_1500_divs",
        DEEP_1500_IN,
        DEEP_1500_OUT,
    ),
    (
        "deep_nesting_1100_spans_with_dropped_script",
        DEEP_SCRIPT_IN,
        DEEP_SCRIPT_OUT,
    ),
]


def test_attack_corpus_size_exceeds_minimum():
    assert len(ATTACK_CORPUS) >= 100
    ids = [case_id for case_id, _, _ in ATTACK_CORPUS]
    assert len(ids) == len(set(ids)), "duplicate case ids in ATTACK_CORPUS"


@pytest.mark.parametrize(
    ("case_id", "raw_input", "expected_html"),
    ATTACK_CORPUS,
    ids=[c[0] for c in ATTACK_CORPUS],
)
def test_attack_corpus_exact_output_allowlist_lxml_and_idempotence(
    case_id: str, raw_input: str, expected_html: str
) -> None:
    res = sanitise_html(raw_input)
    assert res.html == expected_html, case_id
    assert_allowlist_valid(res.html)

    second = sanitise_html(res.html)
    assert second.html == res.html, f"non-idempotent output on {case_id}"
    assert second.text == res.text, f"non-idempotent text on {case_id}"
    assert second.sanitised is None, f"second pass reported removals on {case_id}"


# ---------------------------------------------------------------------------
# Exhaustive sweep over all DOM/HTML on* event handler attributes
# ---------------------------------------------------------------------------

ALL_EVENT_HANDLERS = (
    "onabort", "onafterprint", "onanimationcancel", "onanimationend",
    "onanimationiteration", "onanimationstart", "onauxclick", "onbeforecopy",
    "onbeforecut", "onbeforeinput", "onbeforepaste", "onbeforeprint",
    "onbeforetoggle", "onbeforeunload", "onbegin", "onblur", "oncancel",
    "oncanplay", "oncanplaythrough", "onchange", "onclick", "onclose",
    "onCommand", "oncontextlost", "oncontextmenu", "oncontextrestored",
    "oncopy", "oncuechange", "oncut", "ondblclick", "ondrag", "ondragend",
    "ondragenter", "ondragleave", "ondragover", "ondragstart", "ondrop",
    "ondurationchange", "onemptied", "onend", "onended", "onerror", "onfocus",
    "onfocusin", "onfocusout", "onformdata", "onfullscreenchange",
    "ongotpointercapture", "onhashchange", "oninput", "oninvalid",
    "onkeydown", "onkeypress", "onkeyup", "onlanguagechange", "onload",
    "onloadeddata", "onloadedmetadata", "onloadstart", "onlostpointercapture",
    "onmessage", "onmessageerror", "onmousedown", "onmouseenter",
    "onmouseleave", "onmousemove", "onmouseout", "onmouseover", "onmouseup",
    "onmousewheel", "onoffline", "ononline", "onpagehide", "onpageshow",
    "onpaste", "onpause", "onplay", "onplaying", "onpointercancel",
    "onpointerdown", "onpointerenter", "onpointerleave", "onpointermove",
    "onpointerout", "onpointerover", "onpointerup", "onpopstate",
    "onprogress", "onratechange", "onrejectionhandled", "onrepeat", "onreset",
    "onresize", "onscroll", "onscrollend", "onsearch",
    "onsecuritypolicyviolation", "onseeked", "onseeking", "onselect",
    "onselectionchange", "onselectstart", "onslotchange", "onstalled",
    "onstorage", "onsubmit", "onsuspend", "ontimeupdate", "ontoggle",
    "ontouchcancel", "ontouchend", "ontouchmove", "ontouchstart",
    "ontransitioncancel", "ontransitionend", "ontransitionrun",
    "ontransitionstart", "onunhandledrejection", "onunload", "onvolumechange",
    "onwaiting", "onwebkitanimationend", "onwebkittransitionend", "onwheel",
)


def test_every_on_event_handler_is_dropped_and_counted():
    for handler in ALL_EVENT_HANDLERS:
        res = sanitise_html(f'<div {handler}="alert(1)">ok</div>')
        assert res.html == "<html><body><div>ok</div></body></html>", handler
        assert res.sanitised == {
            "removed_elements": [],
            "removed_attributes": 1,
            "removed_links": 0,
        }
        assert_allowlist_valid(res.html)


# ---------------------------------------------------------------------------
# 2. 5,400 seeded random fuzz inputs (soup, nested trees, seed mutations)
#    including malformed marked sections, bracketed hosts, and unterminated
#    constructs — checked with independent grammar + lxml.html + idempotence.
# ---------------------------------------------------------------------------

_FZ_ALLOWED = tuple(sorted(SPEC_ELEMENTS))
_FZ_DROPPED = (
    "script", "style", "head", "title", "meta", "link", "base", "object", "embed",
    "iframe", "frame", "frameset", "applet", "form", "input", "button", "select",
    "option", "textarea", "svg", "math", "template", "noscript", "audio", "video",
    "source", "track", "canvas", "map", "area", "img",
)
_FZ_UNKNOWN = (
    "xmp", "plaintext", "listing", "noembed", "marquee", "center", "bdo", "bdi",
    "section", "article", "body", "html", "foreignObject", "mtext", "mglyph",
    "annotation-xml", "x-custom", "o:p", "w:worddocument",
)
_FZ_ALL_TAGS = _FZ_ALLOWED + _FZ_DROPPED + _FZ_UNKNOWN
_FZ_WS = (" ", " ", "\t", "\n", "\r", "\x0c", "/", "  ", "\x00", "")
_FZ_CTRL = (
    "\x00", "\x01", "\x09", "\x0a", "\x0c", "\x0d", "\x7f", "\u00a0", "\u00ad",
    "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202e", "\u2060",
    "\u2066", "\u2069", "\u3000", "\ufeff", "\ufffe", "\uffff", "\u034f", "\u061c",
    "\u180b", "\u2800", "\u3164", "\ufe0f",
)
_FZ_HREFS = (
    "javascript:alert(1)", "JaVaScRiPt:x", "vbscript:x", "data:text/html,<script>x</script>",
    "&#106;avascript:x", "&Tab;javascript:x", "https://example.com/a?lang=ar&region=sa&copy=1",
    "https://[", "https://[example.com]/", "https://[::1]/ok", "https://example.com\uff0fx",
    "https://portal.example.com%2eevil.example/", "https://portal.example.com\u3002evil.example/",
    "https://user:pw@example.com/", "mailto:team@example.com",
    "mailto:team@example.com?bcc=spy@evil.example", "mailto:a@example.com,b@evil.example",
    "mailto: ", "//evil.example/x", "/relative", "https://xn--e1afmkfd.example/",
)
_FZ_STYLES = (
    "color: red", "background: url(https://evil.example/x)", "background-color: #f0f0f0",
    "width: expression(alert(1))", "font-family: 'Traditional Arabic',\n Tahoma",
    "font-size: 12pt", "font-size: 0", "font-size: 3px", "direction: rtl",
    "unicode-bidi: bidi-override", "unicode-bidi: embed", "text-align: right",
    "@import 'x'", "color: r\\65 d", "color: /* c */ red", "border: 1px solid #ccc",
    "position: fixed", "display: none", "line-height: 1.6",
)
_FZ_ATTRS = (
    "dir", "lang", "align", "style", "href", "title", "colspan", "rowspan",
    "width", "height", "border", "face", "size", "color", "start", "type", "span",
    "onclick", "onerror", "onload", "src", "action", "formaction", "xlink:href",
    "id", "class", "HREF", "Style", "on\x00click",
)
_FZ_VALS = (
    "rtl", "ltr", "auto", "right", "center", "ar", "en-US", "1", "2", "0", "100%",
    "50px", "+2", "red", "#abcdef", "Tahoma", "i", "", '"', "'", "<script>",
    "</a><script>x()</script>", "--><script>", "مرحبا",
)
_FZ_WORDS = (
    "hello ", "world ", "السلام عليكم ", "١٢٣ ", "1 < 2 ", "a > b ", "x & y ",
    "portal.example.com ", "]]>", "-->", "<!--", "--!>", "<![x]>", "<![CDAT[x]]>",
    "<![CDATA[x]]>", "<![if !IE]>", "<?pi x?>", "<!DOCTYPE html>", "</", "<",
    "<>", "<3", "<p", "<a href=\"", "<!-->", "<!--->",
)
_FZ_SEEDS = (
    '<html dir="rtl" lang="ar"><head><meta charset="utf-8"><title>t</title>'
    '<body><p align="right">السلام عليكم</p><ul><li>أول<li>ثاني</ul>'
    '<table border="1"><thead><tr><th>بند<tbody><tr><td>قيمة</table></body></html>',
    '<p>hello</p><![x]><![CDAT[x]]><a href="https://[">x</a>'
    '<a href="https://[example.com]/">y</a><a href="https://example.com\uff0fx">z</a>',
    '<table><a href="https://example.com">x<tr><td>y</td></tr></a></table>',
    '<p>a<pre>\n\nb</pre><table><caption>c<td>d<col><colgroup>e</table>f',
    '<svg><style><!--</style><img src=x onerror=alert(1)>--></svg><p>ok</p>',
    '<a href="https://example.com" href="javascript:alert(1)">z</a><p a="unclosed>w',
)


def _make_fuzz_attr(rng: random.Random) -> str:
    name = rng.choice(_FZ_ATTRS)
    low = name.lower()
    if low == "href":
        val = rng.choice(_FZ_HREFS)
    elif low == "style":
        val = "; ".join(rng.choice(_FZ_STYLES) for _ in range(rng.randint(1, 3)))
    else:
        val = rng.choice(_FZ_VALS)
    r = rng.random()
    if r < 0.10:
        return name
    if r < 0.60:
        return f'{name}="{val}"'
    if r < 0.85:
        return f"{name}='{val}'"
    if r < 0.95:
        return f"{name}={val}"
    return f"{name}=`{val}`"


def _make_fuzz_open(rng: random.Random, tag: str | None = None) -> str:
    t = tag or rng.choice(_FZ_ALL_TAGS)
    if rng.random() < 0.2:
        t = t.upper()
    attrs = [_make_fuzz_attr(rng) for _ in range(rng.choice((0, 0, 1, 2, 3)))]
    s = "<" + t + "".join((rng.choice(_FZ_WS) or " ") + a for a in attrs)
    r = rng.random()
    if r < 0.82:
        return s + ">"
    if r < 0.92:
        return s + "/>"
    return s  # unterminated


def _make_fuzz_close(rng: random.Random, tag: str | None = None) -> str:
    t = tag or rng.choice(_FZ_ALL_TAGS)
    tail = rng.choice((">", ">", " x=y>", "/>", ""))
    return f"</{t}{tail}"


def _gen_fuzz_tree(rng: random.Random, depth: int = 0) -> str:
    if depth > 4 or rng.random() < 0.30:
        return rng.choice(_FZ_WORDS + _FZ_CTRL)
    tag = rng.choice(_FZ_ALLOWED * 3 + _FZ_DROPPED + _FZ_UNKNOWN)
    inner = "".join(_gen_fuzz_tree(rng, depth + 1) for _ in range(rng.randint(0, 3)))
    close = _make_fuzz_close(rng, tag) if rng.random() < 0.8 else ""
    return _make_fuzz_open(rng, tag) + inner + close


def _gen_fuzz_case(rng: random.Random, idx: int) -> str:
    mode = idx % 3
    if mode == 0:
        parts: list[str] = []
        for _ in range(rng.randint(1, 24)):
            r = rng.random()
            if r < 0.32:
                parts.append(_make_fuzz_open(rng))
            elif r < 0.52:
                parts.append(_make_fuzz_close(rng))
            elif r < 0.85:
                parts.append(rng.choice(_FZ_WORDS))
            else:
                parts.append(rng.choice(_FZ_CTRL))
        return "".join(parts)
    if mode == 1:
        return _gen_fuzz_tree(rng)
    s = rng.choice(_FZ_SEEDS)
    for _ in range(rng.randint(1, 6)):
        pos = rng.randrange(len(s) + 1)
        if rng.random() < 0.5:
            s = s[:pos] + rng.choice(_FZ_WORDS + _FZ_CTRL) + s[pos:]
        else:
            end = min(len(s), pos + rng.randint(1, 8))
            s = s[:pos] + s[end:]
    return s


def _gen_wrapper_around_block_fuzz_case(rng: random.Random) -> str:
    """Generate removable wrappers (empty anchors, anchors with nested anchors,
    or unknown tags) around blocks inside pre, ul, ol, p, or a table cell."""
    inner_blocks = (
        "<ul></ul>",
        "<ol><hr></ol>",
        "<ul><li></li></ul>",
        "<p></p>",
        "<div><hr></div>",
        "<table><tr><td></td></tr></table>",
        "<pre></pre>",
        "<blockquote></blockquote>",
        "<h2></h2>",
        "<hr>",
        "<table><tr><td><a href=\"https://b.example/\">y</a></td></tr></table>",
    )
    block = rng.choice(inner_blocks)
    if rng.random() < 0.35:
        block = block + rng.choice(inner_blocks)

    wrappers = (
        ('<a href="https://x.example/">', "</a>"),
        ('<a href="javascript:alert(1)">', "</a>"),
        ("<a>", "</a>"),
        ("<section>", "</section>"),
        ("<custom-wrap>", "</custom-wrap>"),
        ('<a href="https://x.example/"><section>', "</section></a>"),
        ('<a href="https://x.example/"><a href="https://y.example/">', "</a></a>"),
    )
    w_open, w_close = rng.choice(wrappers)
    wrapped = f"{w_open}{block}{w_close}"
    before = rng.choice(("", "t", "<span>t</span>"))
    after = rng.choice(("t", "", "<b>k</b>"))

    container = rng.choice(("pre", "ul", "ol", "p", "td"))
    if container == "pre":
        return f"<pre>{before}{wrapped}{after}</pre>"
    if container == "ul":
        return f"<ul><li>x</li>{wrapped}{after}</ul>"
    if container == "ol":
        return f"<ol><li>x</li>{wrapped}{after}</ol>"
    if container == "p":
        return f"<p>{before or 'x'}{wrapped}{after}</p>"
    return f"<table><tr><td>{before or 'x'}{wrapped}{after}</td></tr></table>"


def test_3000_seeded_fuzz_cases_agree_with_lxml_allowlist_and_idempotence():
    rng = random.Random(2026111602)
    accepted = 0
    refused = 0

    for idx in range(3600):
        raw = (
            _gen_fuzz_case(rng, idx)
            if idx < 3000
            else _gen_wrapper_around_block_fuzz_case(rng)
        )
        if len(raw) > MAX_HTML_CHARS:
            raw = raw[:MAX_HTML_CHARS]
        try:
            res = sanitise_html(raw)
        except HTMLValidationError:
            refused += 1
            continue

        accepted += 1
        tsum = strict_token_summary(res.html)
        lx = lxml_tree_summary(res.html)
        assert lx["tags"] == tsum["tags"], f"case {idx}: {raw!r}"
        assert lx["block_parents"] == tsum["block_parents"], f"case {idx}: {raw!r}"
        assert "".join(lx["text"].split()) == "".join(tsum["text"].split()), f"case {idx}"

        if len(res.html) <= MAX_HTML_CHARS:
            again = sanitise_html(res.html)
            assert again.html == res.html, f"case {idx} not idempotent: {raw!r} -> {res.html!r}"
            assert again.text == res.text, f"case {idx} text not idempotent"
            assert again.sanitised is None, f"case {idx} second pass reported removals"

        info = inspect_draft_links(res.html, limit=10**9)
        lx_hrefs = sorted({h for h, _ in lx["links"]})
        in_hrefs = sorted({d["href"] for d in info["links"]})
        assert lx_hrefs == in_hrefs, f"case {idx} link href mismatch: {lx_hrefs} vs {in_hrefs}"

    assert accepted >= 1800
    assert refused >= 150


# ---------------------------------------------------------------------------
# 3. All 58 deceptive link variants + 19 controls from links_probe.py
# ---------------------------------------------------------------------------

_EVIL_URL = "https://evil.example/login"


def _anchor_p(text: str, href: str = _EVIL_URL, extra: str = "") -> str:
    return f'<p><a href="{href}"{extra}>{text}</a></p>'


LINKS_PROBE_CASES: list[tuple[str, str, str]] = [
    # 19 controls
    ("control", "bare host", _anchor_p("portal.example.com")),
    ("control", "explicit url", _anchor_p("https://portal.example.com/login")),
    ("control", "host with path", _anchor_p("portal.example.com/login")),
    ("control", "host with port", _anchor_p("portal.example.com:8443")),
    ("control", "host with port and path", _anchor_p("portal.example.com:8443/login")),
    ("control", "host with query", _anchor_p("portal.example.com?x=1")),
    ("control", "www host", _anchor_p("www.portal.example.com")),
    ("control", "upper case", _anchor_p("PORTAL.EXAMPLE.COM")),
    ("control", "split across <b>", _anchor_p("portal.<b>exam</b>ple.<i>com</i>")),
    ("control", "numeric entities", _anchor_p("portal&#46;example&#46;com")),
    ("control", "zero width space", _anchor_p("portal\u200b.example.com")),
    ("control", "soft hyphen", _anchor_p("por\u00adtal.example.com")),
    ("control", "word joiner", _anchor_p("portal.exa\u2060mple.com")),
    ("control", "email text, web target", _anchor_p("billing@portal.example.com")),
    (
        "control",
        "email text, other mailto",
        _anchor_p("billing@portal.example.com", "mailto:attacker@evil.example"),
    ),
    ("control", "trailing dot/comma", _anchor_p("portal.example.com.,")),
    ("control", "parenthesised", _anchor_p("(portal.example.com)")),
    ("control", "cyrillic look-alike host in text", _anchor_p("port\u0430l.example.com")),
    ("control", "ipv4 text", _anchor_p("10.1.2.3:8080/x")),
    # 58 deceptive evasion variants
    ("second token", "host + arrow", _anchor_p("portal.example.com \u203a")),
    ("second token", "host + (secure)", _anchor_p("portal.example.com (secure)")),
    ("second token", "Go to host", _anchor_p("Go to portal.example.com")),
    ("second token", "www host - sign in", _anchor_p("www.portal.example.com - sign in")),
    ("second token", "host/path + word", _anchor_p("portal.example.com/login now")),
    ("second token", "host + nbsp + word", _anchor_p("portal.example.com&nbsp;login")),
    (
        "second token",
        "Arabic sentence with host",
        _anchor_p("\u0627\u0644\u062f\u062e\u0648\u0644 \u0639\u0628\u0631 portal.example.com"),
    ),
    (
        "second token",
        "email + display name",
        _anchor_p("Billing &lt;billing@portal.example.com&gt;"),
    ),
    ("second token", "email + word", _anchor_p("billing@portal.example.com (accounts)")),
    ("punctuation", "curly quotes", _anchor_p("\u201cportal.example.com\u201d")),
    ("punctuation", "guillemets", _anchor_p("\u00abportal.example.com\u00bb")),
    ("punctuation", "braces", _anchor_p("{portal.example.com}")),
    ("punctuation", "trailing ellipsis char", _anchor_p("portal.example.com\u2026")),
    ("punctuation", "trailing asterisk", _anchor_p("portal.example.com*")),
    ("punctuation", "leading arrow no space", _anchor_p("\u2192portal.example.com")),
    ("punctuation", "backslash path", _anchor_p("portal.example.com\\login")),
    ("punctuation", "pipe", _anchor_p("|portal.example.com|")),
    ("invisible", "combining grapheme joiner U+034F", _anchor_p("portal.exa\u034fmple.com")),
    ("invisible", "variation selector U+FE0F", _anchor_p("portal.example.com\ufe0f")),
    ("invisible", "braille blank U+2800", _anchor_p("portal.example.com\u2800")),
    ("invisible", "noncharacter U+FFFE", _anchor_p("portal.exa\ufffemple.com")),
    ("invisible", "mongolian FVS U+180B", _anchor_p("portal.exa\u180bmple.com")),
    ("invisible", "hangul filler U+3164", _anchor_p("portal.example.com\u3164")),
    ("look-alike", "fullwidth dots U+FF0E", _anchor_p("portal\uff0eexample\uff0ecom")),
    ("look-alike", "one dot leader U+2024", _anchor_p("portal\u2024example\u2024com")),
    ("look-alike", "ideographic stop U+3002", _anchor_p("portal\u3002example\u3002com")),
    (
        "look-alike",
        "url with fraction slashes",
        _anchor_p("https:\u2044\u2044portal.example.com\u2044login"),
    ),
    (
        "look-alike",
        "url with fullwidth colon",
        _anchor_p("https\uff1a//portal.example.com/login"),
    ),
    (
        "look-alike",
        "url with division slashes",
        _anchor_p("https:\u2215\u2215portal.example.com"),
    ),
    (
        "look-alike",
        "url, cyrillic in scheme",
        _anchor_p("htt\u0440s://portal.example.com/login"),
    ),
    ("look-alike", "url scheme hxxps-like 'https;//'", _anchor_p("https;//portal.example.com")),
    ("bidi", "RLO + reversed url with path", _anchor_p("\u202enigol/moc.elpmaxe.latrop//:sptth")),
    ("bidi", "RLO + reversed host/path", _anchor_p("\u202enigol/moc.elpmaxe.latrop")),
    ("bidi", "RLO + reversed bare host (accidental)", _anchor_p("\u202emoc.elpmaxe.latrop")),
    (
        "bidi",
        "css bidi-override reversed",
        _anchor_p(
            '<span style="unicode-bidi: bidi-override; direction: rtl">'
            "nigol/moc.elpmaxe.latrop</span>"
        ),
    ),
    (
        "bidi",
        "RLI isolate reversed",
        _anchor_p("\u2067\u202enigol/moc.elpmaxe.latrop\u202c\u2069"),
    ),
    ("markup", "stray <tr> (ignored by HTML5 in body)", _anchor_p("portal.example<tr></tr>.com")),
    (
        "markup",
        "stray <tr> inside url scheme",
        _anchor_p("https:<tr></tr>//portal.example.com/login"),
    ),
    (
        "markup",
        "hidden second token font-size 0",
        _anchor_p('portal.example.com<span style="font-size: 0"> x</span>'),
    ),
    (
        "markup",
        "hidden second token white on white",
        _anchor_p('portal.example.com<span style="color: #ffffff; font-size: 1px"> x</span>'),
    ),
    (
        "markup",
        "hidden second token <font size>",
        _anchor_p('portal.example.com<font color="white" size="1"> x</font>'),
    ),
    (
        "markup",
        "three adjacent anchors",
        f'<p><a href="{_EVIL_URL}">portal.</a>'
        f'<a href="{_EVIL_URL}">example.</a><a href="{_EVIL_URL}">com</a></p>',
    ),
    (
        "markup",
        "two anchors, tail to evil",
        '<p><a href="https://portal.example.com/">portal.example</a>'
        f'<a href="{_EVIL_URL}">.com</a></p>',
    ),
    (
        "markup",
        "title attribute shows trusted url",
        _anchor_p("Sign in", extra=' title="https://portal.example.com/login"'),
    ),
    (
        "markup",
        "link wraps a table cell",
        f'<a href="{_EVIL_URL}"><table><tr><td>portal.example.com</td>'
        "<td>ok</td></tr></table></a>",
    ),
    (
        "address",
        "email text, web page on SAME host",
        _anchor_p("billing@portal.example.com", "https://portal.example.com/collect"),
    ),
    (
        "address",
        "email text, mailto adds cc",
        _anchor_p(
            "billing@portal.example.com",
            "mailto:billing@portal.example.com?cc=attacker@evil.example",
        ),
    ),
    (
        "address",
        "email text, mailto adds to=",
        _anchor_p(
            "billing@portal.example.com",
            "mailto:billing@portal.example.com?to=attacker@evil.example",
        ),
    ),
    (
        "address",
        "email text, mailto adds bcc + body",
        _anchor_p(
            "billing@portal.example.com",
            "mailto:billing@portal.example.com?bcc=attacker@evil.example&amp;body=secret",
        ),
    ),
    (
        "address",
        "email text, mailto with second addr",
        _anchor_p(
            "billing@portal.example.com",
            "mailto:billing@portal.example.com,attacker@evil.example",
        ),
    ),
    (
        "address",
        "email text with unicode domain letter",
        _anchor_p("billing@port\u0430l.example.com"),
    ),
    (
        "address",
        "url text, mailto on same domain",
        _anchor_p("https://portal.example.com/login", "mailto:phish@portal.example.com"),
    ),
    (
        "address",
        "url text with userinfo trick",
        _anchor_p("https://portal.example.com@evil.example/"),
    ),
    (
        "address",
        "url text https, target http same host",
        _anchor_p("https://portal.example.com/login", "http://portal.example.com:8080/x"),
    ),
    ("address", "host on file-extension TLD (.zip)", _anchor_p("portal-login.zip")),
    (
        "address",
        "neutral text, IDN look-alike target",
        _anchor_p("Portal", "https://port\u0430l.example.com/"),
    ),
    (
        "address",
        "neutral text, percent-encoded dot target",
        _anchor_p("Portal", "https://portal.example.com%2eevil.example/"),
    ),
    (
        "address",
        "neutral text, ideographic-dot target",
        _anchor_p("Portal", "https://portal.example.com\u3002evil.example/"),
    ),
]


def test_links_probe_matrix_counts():
    controls = [c for c in LINKS_PROBE_CASES if c[0] == "control"]
    evasions = [c for c in LINKS_PROBE_CASES if c[0] != "control"]
    assert len(controls) == 19
    assert len(evasions) == 58


@pytest.mark.parametrize(
    ("group", "desc", "markup"),
    LINKS_PROBE_CASES,
    ids=[f"{g}:{d}" for g, d, _ in LINKS_PROBE_CASES],
)
def test_links_probe_raw_and_sanitised_never_evade_warning(
    group: str, desc: str, markup: str
) -> None:
    is_neutral_target_probe = desc.startswith("neutral text,")
    is_mailto_probe = "mailto:" in markup.lower()

    def is_mismatch_tier_warning(warning: str) -> bool:
        if "link mismatch" in warning:
            return True
        if is_mailto_probe and (
            "mailto link specifies multiple recipients:" in warning
            or "mailto link carries query parameters:" in warning
        ):
            return True
        return False

    # 1. Direct inspection of unsanitised stored HTML (e.g. quoted reply/forward):
    #    every deceptive-text variant must emit a mismatch-tier warning (with
    #    mailto: probes satisfied by either of the two mailto destination warnings
    #    or by a link mismatch warning), and neutral-text target probes must warn
    #    on the IDN/suspicious target host.
    raw_info = inspect_draft_links(f"<html><body>{markup}</body></html>")
    if is_neutral_target_probe:
        assert any(
            "non-ASCII or punycode link host" in w or "suspicious encoded or look-alike host" in w
            for w in raw_info["warnings"]
        ), f"raw target probe missed {group}/{desc}: {raw_info}"
    else:
        assert any(is_mismatch_tier_warning(w) for w in raw_info["warnings"]), (
            f"raw inspect missed mismatch warning on {group}/{desc}: {raw_info}"
        )

    # 2. End-to-end through sanitise_html -> inspect_draft_links:
    #    either sanitise_html removes the deceptive link/part (removed_links >= 1
    #    and no deceptive destination remains) or inspect_draft_links emits the
    #    mismatch-tier warning (or host warning on the neutral-text IDN probe).
    res = sanitise_html(markup)
    assert_allowlist_valid(res.html)
    stored_info = inspect_draft_links(res.html)
    link_removed = (
        res.sanitised is not None
        and res.sanitised["removed_links"] >= 1
        and "evil.example" not in res.html
    )
    has_mismatch_warning = any(
        is_mismatch_tier_warning(w) for w in stored_info["warnings"]
    )

    if is_neutral_target_probe:
        has_host_warning = any(
            "non-ASCII or punycode link host" in w for w in stored_info["warnings"]
        )
        assert has_host_warning or link_removed, (
            f"neutral-text target probe {desc} evaded warning: "
            f"stored={res.html!r} info={stored_info}"
        )
    elif group == "control":
        assert has_mismatch_warning, (
            f"control {desc} did not emit mismatch warning: {stored_info}"
        )
    else:
        assert has_mismatch_warning or link_removed, (
            f"evasion {desc} passed without mismatch warning or link removal: "
            f"stored={res.html!r} info={stored_info}"
        )


# ---------------------------------------------------------------------------
# 4. Stored-HTML extractor differentials (extractor_diff.py constructs &
#    non-anchor URL attributes, host caps, warning caps, unverified flag)
# ---------------------------------------------------------------------------

_DIFF_LINK = '<a href="https://evil.example/login">https://portal.example.com/login</a>'

EXTRACTOR_CONSTRUCTS: list[tuple[str, str, str]] = [
    ("", "", "yes"),
    ("<![x]>", "", "yes"),
    ("<![ endif]>", "", "yes"),
    ("<![CDATA[>", "]]>", "yes"),
    ("<![CDATA[x]]>", "", "yes"),
    ("<!-->", "<!-- -->", "yes"),
    ("<!--->", "<!-- -->", "yes"),
    ("<!-- --!>", "<!-- -->", "yes"),
    ("<!-- x --!>", "-->", "yes"),
    ("<!--", "", "no"),
    ("<!-- <!-- -->", "-->", "yes"),
    ("<? >", "?>", "yes"),
    ('<!x ">', '">', "yes"),
    ("<head>", "</head>", "yes"),
    ("<head><title>t</title>", "</head>", "yes"),
    ("<noscript>", "</noscript>", "yes"),
    ("<template>", "</template>", "yes_fail_closed"),
    ("<title>", "</title>", "no"),
    ("<title><!--</title>-->", "", "yes"),
    ("<title></title x>", "", "yes"),
    ("<title></title/>", "", "yes"),
    ("<script></script x>", "", "yes"),
    ("<script></script/>", "", "yes"),
    ("<script></script\n>", "", "yes"),
    ("<script><!--</script>-->", "", "yes"),
    ("<script/>", "</script>", "no"),
    ("<style></style x>", "", "yes"),
    ("<style></style/>", "", "yes"),
    ("<style/>", "</style>", "no"),
    ("<style><!--</style>-->", "", "yes"),
    ("<textarea>", "</textarea>", "no"),
    ("<xmp>", "</xmp>", "no"),
    ("<iframe>", "</iframe>", "no"),
    ("<noembed>", "</noembed>", "no"),
    ("<plaintext>", "", "no"),
    ("<svg>", "</svg>", "yes"),
    ("<svg><title>", "</title></svg>", "yes"),
    ("<svg><style>", "</style></svg>", "yes"),
    ("<math><style>", "</style></math>", "yes"),
    ("<select>", "</select>", "no"),
    ("<table><tr><td>", "</td></tr></table>", "yes"),
    ("<p title='<head>'>", "</p>", "yes"),
    ('<p title="<!--">', "</p>", "yes"),
    ("<p title='<script>'>", "</p>", "yes"),
    ("<a name=x>", "", "yes"),
    ("<a href=''>", "", "yes"),
    ("<a href=' '>", "", "yes"),
    ("<a>", "", "yes"),
    ("</a", ">", "html5_end_tag_consumes"),
    ("<p <!-- x >", "", "yes"),
    ("<p a='", "' >", "quoted_attr"),
    ("<base href='https://evil.example/'>", "", "yes"),
]

EXTRACTOR_OTHER_LINKS: list[tuple[str, str, bool]] = [
    (
        "area",
        '<map name="m"><area shape="rect" coords="0,0,9,9" '
        'href="https://evil.example/login"></map>',
        True,
    ),
    (
        "svg a xlink:href",
        '<svg><a xlink:href="https://evil.example/login">'
        "<text>portal.example.com</text></a></svg>",
        False,
    ),
    (
        "svg a href",
        '<svg><a href="https://evil.example/login"><text>portal.example.com</text></a></svg>',
        False,
    ),
    (
        "math href",
        '<math href="https://evil.example/login"><mtext>portal.example.com</mtext></math>',
        True,
    ),
    (
        "form action + submit",
        '<form action="https://evil.example/login">'
        '<input type="submit" value="portal.example.com"></form>',
        True,
    ),
    (
        "button formaction",
        '<form><button formaction="https://evil.example/login">'
        "portal.example.com</button></form>",
        True,
    ),
    (
        "base + relative a",
        '<base href="https://evil.example/"><a href="/login">portal.example.com</a>',
        False,
    ),
    (
        "A HREF upper case",
        '<A HREF="https://evil.example/login">https://portal.example.com/login</A>',
        False,
    ),
    (
        "a href duplicate (first wins)",
        '<a href="https://evil.example/login" href="https://portal.example.com/">'
        "https://portal.example.com/login</a>",
        False,
    ),
    (
        "a href with tab inside",
        '<a href="https://ev\til.example/login">https://portal.example.com/login</a>',
        False,
    ),
    (
        "a href entity-encoded",
        '<a href="&#104;ttps&colon;//evil&period;example/login">'
        "https://portal.example.com/login</a>",
        False,
    ),
    (
        "a href backslashes",
        '<a href="https:\\\\evil.example\\login">https://portal.example.com/login</a>',
        False,
    ),
    (
        "a href protocol-relative",
        '<a href="//evil.example/login">https://portal.example.com/login</a>',
        False,
    ),
    (
        "a href userinfo",
        '<a href="https://portal.example.com@evil.example/login">'
        "https://portal.example.com/login</a>",
        False,
    ),
    (
        "a href javascript",
        '<a href="javascript:alert(1)">https://portal.example.com/login</a>',
        False,
    ),
]


@pytest.mark.parametrize(
    ("prefix", "suffix", "expectation"),
    EXTRACTOR_CONSTRUCTS,
    ids=[f"{idx}:{ascii(p)[:24]}" for idx, (p, _, _) in enumerate(EXTRACTOR_CONSTRUCTS)],
)
def test_extractor_diff_constructs_never_hide_rendered_links(
    prefix: str, suffix: str, expectation: str
) -> None:
    doc = f"<html><body><p>new text</p><div>{prefix}{_DIFF_LINK}{suffix}</div></body></html>"
    info = inspect_draft_links(doc)
    if expectation.startswith("yes"):
        assert any("evil.example" in item["href"] for item in info["links"]), (prefix, info)
        assert any(h["host"] == "evil.example" for h in info["link_hosts"]), (prefix, info)
        assert info["warnings"], (prefix, info)


@pytest.mark.parametrize(
    ("name", "markup", "expect_embedded"),
    EXTRACTOR_OTHER_LINKS,
    ids=[n for n, _, _ in EXTRACTOR_OTHER_LINKS],
)
def test_extractor_other_url_attributes_and_active_elements(
    name: str, markup: str, expect_embedded: bool
) -> None:
    doc = f"<html><body><p>new text</p>{markup}</body></html>"
    info = inspect_draft_links(doc)
    assert info["warnings"], (name, info)
    if name != "a href javascript":
        evil_entries = [h for h in info["link_hosts"] if h["host"] == "evil.example"]
        assert len(evil_entries) == 1, (name, info)
        assert bool(evil_entries[0].get("embedded")) is expect_embedded, (name, info)


def test_inspect_draft_links_embedded_src_poster_data_cite_background():
    doc = (
        "<html><body background='https://bg.example.com/t.png'>"
        "<img src='https://img.example.com/p.png'>"
        "<video poster='https://vid.example.com/f.jpg'></video>"
        "<object data='https://obj.example.com/x.bin'></object>"
        "<blockquote cite='https://cite.example.com/ref'>q</blockquote>"
        "<a href='https://img.example.com/page'>Visit</a>"
        "</body></html>"
    )
    info = inspect_draft_links(doc)
    by_host = {h["host"]: h for h in info["link_hosts"]}
    assert set(by_host) == {
        "bg.example.com",
        "cite.example.com",
        "img.example.com",
        "obj.example.com",
        "vid.example.com",
    }
    # Hosts only referenced via non-anchor attrs are marked embedded=True;
    # img.example.com is also an <a href> target so embedded is omitted.
    assert by_host["bg.example.com"].get("embedded") is True
    assert by_host["vid.example.com"].get("embedded") is True
    assert by_host["obj.example.com"].get("embedded") is True
    assert by_host["cite.example.com"].get("embedded") is True
    assert "embedded" not in by_host["img.example.com"]
    assert by_host["img.example.com"]["count"] == 2


def test_inspect_draft_links_punycode_and_idn_hosts_show_ascii_and_warn():
    written_portal = "port\u0430l.example.com"
    expected_portal = written_portal.encode("idna").decode("ascii")
    written_ru = "\u043f\u0440\u0438\u043c\u0435\u0440.example.com"
    expected_ru = written_ru.encode("idna").decode("ascii")
    assert expected_portal.startswith("xn--")
    assert expected_portal != "portal.example.com"

    doc = (
        "<html><body>"
        f'<a href="https://{written_portal}/a">Portal A</a>'
        f'<a href="https://{expected_ru}/b">Portal B</a>'
        "</body></html>"
    )
    info = inspect_draft_links(doc)
    by_host = {h["host"]: h for h in info["link_hosts"]}
    assert expected_portal in by_host
    assert by_host[expected_portal]["as_written"] == written_portal
    assert expected_ru in by_host
    assert by_host[expected_ru]["as_written"] == written_ru
    assert len(info["warnings"]) == 2
    assert any(expected_portal in w for w in info["warnings"])
    assert any(expected_ru in w for w in info["warnings"])


def test_inspect_draft_links_unparseable_url_is_listed_and_warned_without_raising():
    doc = '<html><body><p><a href="https://[">Broken</a></p></body></html>'
    info = inspect_draft_links(doc)
    assert info["links"] == [{"text": "Broken", "href": "https://["}]
    assert info["warnings"]
    assert "links_unverified" not in info


def test_inspect_draft_links_caps_hosts_warnings_link_text_and_oversize_unverified():
    # 1. 55 anchor hosts + 30 mismatched links: mismatch warnings and the
    #    omitted-hosts notice are never dropped even when exceeding 20 warnings,
    #    and exceeding MAX_PREVIEW_HOSTS sets links_unverified=True.
    long_label = "Sign in now " + ("x" * 160)
    parts = [f'<a href="https://h00.example.com/0">{long_label}</a>']
    for idx in range(1, 55):
        text = f"trusted-{idx:02d}.example.org" if idx <= 30 else f"Doc {idx}"
        parts.append(f'<a href="https://h{idx:02d}.example.com/{idx}">{text}</a>')
    info = inspect_draft_links("<html><body>" + " ".join(parts) + "</body></html>")

    assert len(info["links"]) == MAX_PREVIEW_LINKS
    assert info["links_total"] == 55
    # Mismatching links come first in `links`, so the 20 shown links are all mismatches;
    # inspect with limit=60 to also verify the 120-char text cap on h00.
    full_links_info = inspect_draft_links(
        "<html><body>" + " ".join(parts) + "</body></html>",
        limit=60,
        max_text_chars=MAX_PREVIEW_LINK_TEXT_CHARS,
    )
    h00_entry = next(d for d in full_links_info["links"] if "h00.example.com" in d["href"])
    assert len(h00_entry["text"]) == MAX_PREVIEW_LINK_TEXT_CHARS
    assert len(info["link_hosts"]) == MAX_PREVIEW_HOSTS
    assert info["link_hosts_total"] == 55
    assert info["links_unverified"] is True
    # All 30 mismatch warnings + 1 omitted-hosts notice = 31 critical notices preserved!
    assert len(info["warnings"]) == 31
    assert sum(1 for w in info["warnings"] if "link mismatch" in w) == 30
    assert any("omitted" in w for w in info["warnings"])

    # 2. Flood of Part B: 55 empty anchors to 55 hosts + 1 deceptive link at the end.
    empties_55 = "".join(f'<a href="https://a{i:03d}.flood.example/"></a>' for i in range(55))
    planted_deceptive = (
        '<p><a href="https://zzz-evil.example/login">https://good.example/login</a></p>'
    )
    flood_raw = f"<html><body><p>Hello</p>{empties_55}{planted_deceptive}</body></html>"

    # 2a. Raw inspection: zzz-evil.example is first in links, first in link_hosts,
    #     its mismatch warning and the hosts-omitted notice survive, and the 55
    #     empty-link warnings fold to 3 examples + 1 count line.
    flood_info = inspect_draft_links(flood_raw)
    assert flood_info["links"][0] == {
        "text": "https://good.example/login",
        "href": "https://zzz-evil.example/login",
    }
    assert flood_info["link_hosts"][0] == {"host": "zzz-evil.example", "count": 1}
    assert flood_info["links_unverified"] is True
    assert any(
        "link mismatch" in w and "zzz-evil.example" in w and "good.example" in w
        for w in flood_info["warnings"]
    )
    assert any("omitted" in w for w in flood_info["warnings"])
    assert any("52 more empty-link warning(s)" in w for w in flood_info["warnings"])

    # 2b. Through sanitise_html: all 55 empty anchors are removed and counted in
    #     removed_links, leaving only the 1 real link and its mismatch warning.
    san_flood = sanitise_html(f"<p>Hello</p>{empties_55}{planted_deceptive}")
    assert san_flood.sanitised == {
        "removed_elements": [],
        "removed_attributes": 0,
        "removed_links": 55,
    }
    san_flood_info = inspect_draft_links(san_flood.html)
    assert san_flood_info["links"] == [
        {"text": "https://good.example/login", "href": "https://zzz-evil.example/login"}
    ]
    assert san_flood_info["link_hosts"][0] == {"host": "zzz-evil.example", "count": 1}
    assert any(
        "link mismatch" in w and "zzz-evil.example" in w
        for w in san_flood_info["warnings"]
    )

    # 3. 25 empty anchors + 30 hosts hidden in one comment + 1 planted deceptive link:
    #    the deceptive link, its host, its mismatch warning, the outside-markup
    #    notice, and the omitted-hosts notice all survive every cap.
    empties_25 = "".join(f'<a href="https://a{i:03d}.flood.example/"></a>' for i in range(25))
    comment_30 = "<!-- " + " ".join(f"href=//b{i:03d}.flood.example" for i in range(30)) + " -->"
    comment_flood_doc = (
        f"<html><body><p>Hello</p>{empties_25}{comment_30}{planted_deceptive}</body></html>"
    )
    cf_info = inspect_draft_links(comment_flood_doc)
    assert cf_info["links"][0] == {
        "text": "https://good.example/login",
        "href": "https://zzz-evil.example/login",
    }
    assert cf_info["link_hosts"][0] == {"host": "zzz-evil.example", "count": 1}
    assert cf_info["links_unverified"] is True
    assert any(
        "link mismatch" in w and "zzz-evil.example" in w for w in cf_info["warnings"]
    )
    assert any("outside normal markup" in w for w in cf_info["warnings"])
    assert any("omitted" in w for w in cf_info["warnings"])

    # 4. Oversize HTML (> 2,000,000 chars) sets links_unverified=True with a size
    #    notice and still lists hosts found past 2,000,000 chars via the raw scan.
    huge = (
        "<html><body>"
        + ("a" * (MAX_INSPECT_HTML_CHARS + 10))
        + '<a href="https://beyond-limit.example/path">Late</a>'
        + "</body></html>"
    )
    huge_info = inspect_draft_links(huge)
    assert huge_info["links"] == []
    assert huge_info["links_unverified"] is True
    assert any("truncated" in w for w in huge_info["warnings"])
    assert huge_info["link_hosts"] == [
        {"host": "beyond-limit.example", "count": 1, "unparsed": True}
    ]


# ---------------------------------------------------------------------------
# 5. Linear-time cost bounds (Promise 4: sanitise < 2 s @ 200k, inspect < 5 s @ 2M)
# ---------------------------------------------------------------------------


def _fit_200k(unit: str, n: int = MAX_HTML_CHARS) -> str:
    return unit * (n // len(unit))


def _distinct_cjk(n: int) -> str:
    pool = (
        list(range(0x4E00, 0x9FA6))
        + list(range(0x3400, 0x4DB6))
        + list(range(0xAC00, 0xD7A4))
    )
    return "".join(chr(c) for c in pool[:n])


def _many_anchors_2m(n: int = MAX_INSPECT_HTML_CHARS) -> str:
    parts: list[str] = []
    total = 0
    idx = 0
    while total < n - 40:
        piece = f"<a href=//h{idx}.bc>x{idx}</a>"
        parts.append(piece)
        total += len(piece)
        idx += 1
    return "".join(parts)


def _comment_hosts_2m(n: int = MAX_INSPECT_HTML_CHARS) -> str:
    parts: list[str] = []
    total = 0
    idx = 0
    while total < n - 40:
        piece = f"<!-- href=//h{idx}.bc -->"
        parts.append(piece)
        total += len(piece)
        idx += 1
    return "".join(parts)


_HALF_2M = (MAX_INSPECT_HTML_CHARS - 80) // 2


@pytest.mark.parametrize(
    ("shape_name", "payload"),
    [
        ("unterminated_start_tags", _fit_200k("<a ", MAX_HTML_CHARS - 1) + "x"),
        ("unterminated_attrs", _fit_200k("<a b=c ", MAX_HTML_CHARS - 1) + "x"),
        ("attr_quote_open", '<p a="' + "x" * (MAX_HTML_CHARS - 8) + ">x"),
        ("unclosed_comments", "x" + _fit_200k("<!--", MAX_HTML_CHARS - 1)),
        ("comment_bang_end", "x<!--" + "-" * (MAX_HTML_CHARS - 9) + "--!>"),
        ("deep_open_only", _fit_200k("<b>", MAX_HTML_CHARS - 1) + "x"),
        (
            "deep_open_wrong_close",
            _fit_200k("<b>", MAX_HTML_CHARS // 2)
            + _fit_200k("</i>", MAX_HTML_CHARS // 2 - 1)
            + "x",
        ),
        (
            "deep_open_autoclose",
            _fit_200k("<b>", MAX_HTML_CHARS // 2)
            + _fit_200k("<a></a>", MAX_HTML_CHARS // 2 - 1)
            + "x",
        ),
        ("many_end_tags_unterminated", "x" + _fit_200k("</a", MAX_HTML_CHARS - 1)),
        ("many_bogus_decls", "x" + _fit_200k("<![if ", MAX_HTML_CHARS - 1)),
        ("many_pis", "x" + _fit_200k("<?", MAX_HTML_CHARS - 1)),
        ("lt_storm", "x" + "<" * (MAX_HTML_CHARS - 1)),
        ("style_near_close", "x<style>" + _fit_200k("</", MAX_HTML_CHARS - 8)),
        (
            "href_entity_obfuscated",
            '<a href="'
            + _fit_200k("&#x09;", 1900)
            + 'https://example.com/">x</a>',
        ),
        (
            "style_decls_many",
            '<p style="' + _fit_200k("color:red;", MAX_HTML_CHARS - 20) + '">x</p>',
        ),
        ("san_dropstack", "x" + "<map>" * 20_000 + "</a>" * 24_999),
        ("san_textconcat", ("\U0001F600" + "abc<!>" * 33_000)[:MAX_HTML_CHARS]),
        ("san_textconcat_bmp", ("abc<!>" * 33_333)[:MAX_HTML_CHARS]),
        (
            "san_nfkc_combining",
            '<p><a href="https://x.example/">a' + "\u0301" * 95_000 + "\u0316" * 95_000 + "</a></p>",
        ),
        (
            "san_idna_text",
            '<p><a href="https://x.example/">https://' + _distinct_cjk(12_000) + ".example/</a></p>",
        ),
        (
            "san_wsdot_200k",
            '<p><a href="https://x.example/">a.b x' + (" \u200b" * 99_970) + " x</a></p>",
        ),
    ],
)
def test_sanitise_html_200k_shapes_complete_in_linear_time(
    shape_name: str, payload: str
) -> None:
    assert len(payload) <= MAX_HTML_CHARS
    t0 = time.perf_counter()
    try:
        res = sanitise_html(payload)
        assert res.html.startswith("<html><body")
    except HTMLValidationError:
        pass
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0 * TIME_MARGIN, f"{shape_name} took {elapsed:.2f}s (expected < 2s)"


@pytest.mark.parametrize(
    ("shape_name", "payload"),
    [
        (
            "links_email_label",
            '<a href="https://example.com">'
            + "a" * (MAX_HTML_CHARS - 40)
            + "</a>",
        ),
        (
            "links_host_label",
            '<a href="https://example.com">'
            + _fit_200k("a.", MAX_HTML_CHARS - 40)
            + "</a>",
        ),
        (
            "links_url_label",
            '<a href="https://example.com">https://'
            + "a" * (MAX_HTML_CHARS - 50)
            + "</a>",
        ),
        ("links_unterminated_tags", _fit_200k("<a ", MAX_HTML_CHARS)),
        (
            "links_unique_mismatch",
            "".join(
                f'<a href="https://evil.example/{idx}">p{idx}.example.com</a>'
                for idx in range(MAX_HTML_CHARS // 62)
            ),
        ),
        (
            "links_same_label_unique_href_mismatch",
            "".join(
                f'<a href="https://evil.example/{idx}">portal.example.com</a>'
                for idx in range(3000)
            )[:MAX_HTML_CHARS],
        ),
        (
            "links_adjacent_split_pairs",
            "".join(
                f'<a href="https://evil.example/{idx}">p{idx}.</a>'
                f'<a href="https://evil.example/{idx}">example.com </a>'
                for idx in range(1800)
            )[:MAX_HTML_CHARS],
        ),
        ("backstop_href_eq_30k", " href=" * 30_000),
        ("backstop_href_open_quote_unclosed", '<a href="' + "a" * (MAX_HTML_CHARS - 10)),
        ("backstop_href_open_quote_repeated", _fit_200k(' href="', MAX_HTML_CHARS)),
        ("insp_idna_href", '<p><a href="https://' + _distinct_cjk(12_000) + '.example/">Open</a></p>'),
        (
            "insp_idna_text",
            '<p><a href="https://x.example/">https://' + _distinct_cjk(12_000) + ".example/</a></p>",
        ),
        ("insp_many_anchors_2m", _many_anchors_2m()),
        (
            "insp_host_tokens_text_2m",
            "<a href=https://x.example/>"
            + ("a.bc " * (MAX_INSPECT_HTML_CHARS // 5))[: MAX_INSPECT_HTML_CHARS - 40]
            + "</a>",
        ),
        (
            "insp_url_tokens_text_2m",
            "<a href=https://x.example/>"
            + ("http://a.bc " * (MAX_INSPECT_HTML_CHARS // 12))[: MAX_INSPECT_HTML_CHARS - 40]
            + "</a>",
        ),
        (
            "insp_nonascii_text_2m",
            "<a href=https://x.example/>"
            + ("\u00e9.bc " * (MAX_INSPECT_HTML_CHARS // 5))[: MAX_INSPECT_HTML_CHARS - 40]
            + "</a>",
        ),
        (
            "insp_nonascii_two_adjacent_2m",
            "<a href=https://x.example/>"
            + ("\u00e9.bc " * (_HALF_2M // 5 + 1))[:_HALF_2M]
            + "</a><a href=https://y.example/>"
            + ("\u00e8.bc " * (_HALF_2M // 5 + 1))[:_HALF_2M]
            + "</a>",
        ),
        (
            "insp_nonascii_url_tokens_2m",
            "<a href=https://x.example/>"
            + ("http://\u00e9.bc " * (MAX_INSPECT_HTML_CHARS // 12))[: MAX_INSPECT_HTML_CHARS - 40]
            + "</a>",
        ),
        (
            "insp_nonascii_title_and_text_2m",
            '<a href=https://x.example/ title="'
            + ("\u00e8.bc " * (_HALF_2M // 5 + 1))[:_HALF_2M]
            + '">'
            + ("\u00e9.bc " * (_HALF_2M // 5 + 1))[:_HALF_2M]
            + "</a>",
        ),
        (
            "insp_adjacent_anchor_tokens_2m",
            (
                "<a href=//a.bc>a.bc </a><a href=//b.bc>b.bc </a>"
                * (MAX_INSPECT_HTML_CHARS // 48)
            )[:MAX_INSPECT_HTML_CHARS],
        ),
        ("insp_host_scan_2m", _comment_hosts_2m()),
        (
            "insp_nfkc_2m",
            '<a href="https://x.example/">a' + "\u0301" * 990_000 + "\u0316" * 990_000 + "</a>",
        ),
        (
            "insp_wsdot_after_sanitise_200k",
            sanitise_html(
                '<p><a href="https://x.example/">a.b x' + (" \u200b" * 99_970) + " x</a></p>"
            ).html,
        ),
        (
            "insp_wsdot_title_2m",
            '<p><a href="https://x.example/" title="a.b x' + (" " * 1_999_920) + 'x">t</a></p>',
        ),
    ],
)
def test_inspect_draft_links_200k_shapes_complete_in_linear_time(
    shape_name: str, payload: str
) -> None:
    assert len(payload) <= MAX_INSPECT_HTML_CHARS
    t0 = time.perf_counter()
    info = inspect_draft_links(payload)
    elapsed = time.perf_counter() - t0
    assert isinstance(info["links"], list)
    assert elapsed < 5.0 * TIME_MARGIN, f"{shape_name} took {elapsed:.2f}s (expected < 5s)"


# ---------------------------------------------------------------------------
# 6. Right-to-left formal mail sample, wrapper attributes & Word/MSO paste
# ---------------------------------------------------------------------------


def test_rtl_formal_sample_preserves_structure_wrapper_styles_and_non_latin_text():
    greeting = "السلام عليكم ورحمة الله وبركاته،"
    intro = "نرفق لكم ملخص البنود المعتمدة للمراجعة قبل الاجتماع القادم:"
    item_one = "مراجعة الجدول الزمني للمشروع وتحديث المراحل."
    item_two = "تأكيد جاهزية التقارير الفنية والمالية."
    col_header_one = "البند"
    col_header_two = "الحالة"
    cell_one = "التقرير الأول"
    cell_two = "مكتمل"
    closing = "للاستفسار يرجى المراسلة عبر البريد:"

    sample = (
        '<!DOCTYPE html><html dir="rtl" lang="ar"><head><meta charset="utf-8"><title>t</title>'
        '<body dir="rtl" style="direction: rtl; text-align: right; '
        "font-family: 'خط نسخ',\n  Tahoma, sans-serif; line-height: 1.8\">"
        f"<p align=\"right\">{greeting}</p>"
        f"<p>{intro}</p>"
        "<ul>"
        f"<li>{item_one}</li>"
        f"<li>{item_two}</li>"
        "</ul>"
        '<table border="1" cellpadding="6" cellspacing="0" width="100%" '
        'style="border-collapse: collapse; border: 1px solid #cccccc">'
        "<thead><tr>"
        f'<th align="right" style="background-color: #f5f5f5">{col_header_one}</th>'
        f'<th align="right" style="background-color: #f5f5f5">{col_header_two}</th>'
        "</tr></thead>"
        "<tbody><tr>"
        f"<td>{cell_one}</td>"
        f"<td>{cell_two}</td>"
        "</tr></tbody>"
        "</table>"
        f"<p>{closing} "
        '<a href="mailto:support@example.com" title="البريد المساند">'
        "support@example.com</a></p>"
        "</body></html>"
    )

    res = sanitise_html(sample)
    assert res.sanitised == {
        "removed_elements": ["head"],
        "removed_attributes": 0,
        "removed_links": 0,
    }
    assert_allowlist_valid(res.html)

    assert res.html.startswith(
        '<html><body dir="rtl" lang="ar" style="direction: rtl; text-align: right; '
        "font-family: 'خط نسخ', Tahoma, sans-serif; line-height: 1.8\">"
    )
    assert 'align="right"' in res.html
    assert "<ul>" in res.html and "<li>" in res.html
    assert "<table" in res.html and "<thead>" in res.html and "<tbody>" in res.html
    assert 'href="mailto:support@example.com"' in res.html

    for phrase in (
        greeting, intro, item_one, item_two,
        col_header_one, col_header_two, cell_one, cell_two, closing,
    ):
        assert phrase in res.html
        assert phrase in res.text

    second = sanitise_html(res.html)
    assert second.html == res.html
    assert second.text == res.text
    assert second.sanitised is None


# ---------------------------------------------------------------------------
# 7. Refusals, invisible-blank rejections, and link mismatch controls
# ---------------------------------------------------------------------------


def test_refuses_input_longer_than_200000_chars_and_accepts_exact_boundary():
    at_limit = "<p>" + ("a" * (MAX_HTML_CHARS - 7)) + "</p>"
    assert len(at_limit) == MAX_HTML_CHARS
    ok = sanitise_html(at_limit)
    assert len(ok.text) == MAX_HTML_CHARS - 7

    # Input at 100,000 '&' chars is accepted even though escaped output exceeds 200,000.
    amp_ok = sanitise_html("&" * 100_000)
    assert len(amp_ok.html) > MAX_HTML_CHARS

    over_limit = at_limit + "x"
    assert len(over_limit) == MAX_HTML_CHARS + 1
    with pytest.raises(HTMLValidationError, match="200000"):
        sanitise_html(over_limit)


@pytest.mark.parametrize("empty_body", [
    "",
    "   \n\t  ",
    "<p>   </p><br><hr>",
    "<p>&nbsp;&nbsp;</p>",
    "<p>&#8203;&#8206;&#8207;&#65279;</p>",
    "<p>\u3164\u2800\u034f\ufe0f\u180b\u115f\u1160\uffa0</p>",
    "<p>\u202d\u202e\ufffe\uffff</p>",
    "<script>alert(1)</script>",
    "<style>body { color: red; }</style>",
    '<img src="https://example.com/banner.png" alt="Banner">',
    "<!-- only a comment -->",
    "<svg><style><!--</style><img src=x onerror=alert(1)></svg>",
])
def test_refuses_when_no_visible_text_remains(empty_body: str):
    with pytest.raises(HTMLValidationError, match="no visible text"):
        sanitise_html(empty_body)


def test_inspect_draft_links_dedupes_caps_and_warns_on_mismatches():
    links_markup = "".join(
        f'<p><a href="https://example.com/item/{idx}">Item {idx}</a></p>'
        for idx in range(25)
    )
    extra = (
        '<p><a href="https://example.com/item/0">Item 0</a></p>'
        '<p><a href="https://other.example.org/login">'
        "https://portal.example.com/login</a></p>"
    )
    info = inspect_draft_links(f"<html><body>{links_markup}{extra}</body></html>")
    assert len(info["links"]) == 20
    assert info["links_total"] == 26
    assert info["links"][0] == {
        "text": "https://portal.example.com/login",
        "href": "https://other.example.org/login",
    }
    assert info["links"][1] == {"text": "Item 0", "href": "https://example.com/item/0"}
    assert info["link_hosts"] == [
        {"host": "other.example.org", "count": 1},
        {"host": "example.com", "count": 26},
        {"host": "portal.example.com", "count": 1, "unparsed": True},
    ]
    assert "outside normal markup" in info["warnings"][0]
    assert "portal.example.com" in info["warnings"][0]
    assert "portal.example.com" in info["warnings"][1]
    assert "other.example.org" in info["warnings"][1]


@pytest.mark.parametrize("anchor_html, should_warn", [
    ('<a href="https://example.com/docs">Read documentation</a>', False),
    ('<a href="https://example.com/docs">report.pdf</a>', False),
    ('<a href="https://example.com/docs">example.com</a>', False),
    ('<a href="https://EXAMPLE.COM:443/a">https://example.com/b</a>', False),
    ('<a href="mailto:Support@Example.com">support@example.com</a>', False),
    ('<a href="mailto:Support@Example.com?subject=Help">support@example.com</a>', True),
    ('<a href="https://other.example.com/pay">portal.example.com</a>', True),
    ('<a href="https://other.example.com/pay">www.example.com/pay</a>', True),
    ('<a href="https://other.example.com/pay">Visit https://portal.example.com now</a>', True),
    ('<a href="https://other.example.com/pay">portal\u200b.example.com</a>', True),
    ('<a href="mailto:other@example.org">billing@example.com</a>', True),
    ('<a href="mailto:other@example.com">billing@example.com</a>', True),
    ('<a href="https://other.example.com/collect">billing@example.com</a>', True),
    (
        '<a href="https://portal.example.com@other.example.com/">'
        "https://portal.example.com</a>",
        True,
    ),
    (
        '<a href="https://other.example.com\\@portal.example.com">'
        "https://portal.example.com</a>",
        True,
    ),
])
def test_inspect_draft_links_mismatch_cases(anchor_html: str, should_warn: bool):
    info = inspect_draft_links(f"<html><body><p>{anchor_html}</p></body></html>")
    assert bool(info["warnings"]) is should_warn, (anchor_html, info)


def test_sanitiser_faithfulness_endtag_bare_quote_and_rawtext_elements():
    res_end = sanitise_html('<p>one</p "><p>two</p>')
    assert res_end.html == "<html><body><p>one</p><p>two</p></body></html>"
    assert "one" in res_end.text and "two" in res_end.text
    assert_allowlist_valid(res_end.html)

    res_noembed = sanitise_html('<noembed><m x="</noembed><p>kept</p>')
    assert res_noembed.html == "<html><body><p>kept</p></body></html>"
    assert res_noembed.text == "kept"
    assert_allowlist_valid(res_noembed.html)

    res_noframes = sanitise_html('<noframes><m x="</noframes><p>kept</p>')
    assert res_noframes.html == "<html><body><p>kept</p></body></html>"
    assert res_noframes.text == "kept"

    res_iframe = sanitise_html('<iframe><m x="</iframe><p>kept</p>')
    assert res_iframe.html == "<html><body><p>kept</p></body></html>"
    assert res_iframe.text == "kept"


def test_inspect_draft_links_raw_backstop_reports_unparsed_hosts_only_with_warning():
    doc = (
        "<html><body>"
        '<p><a href="https://normal.example.com/ok">Normal</a></p>'
        "<!-- <a href=\"https://comment.example.com/c\">hidden</a> -->"
        "<noscript><m x=\"</noscript><a href=https://noscript.example.com/n>x</a>"
        '<noscript><img src="</noscript><a href="https://noscript-q.example.com/p">y</a>'
        "<p title='srcset=\"https://srcset-a.example.com/1.png 1x, "
        "https://srcset-b.example.com/2.png 2x\"'>t</p>"
        "<!-- action=`htt&#x70;s://backtick.example.com/f` -->"
        '<p>tail <a href="https://unterminated.example.com/u'
    )
    info = inspect_draft_links(doc)
    # Backstop never adds to `links`: only real parsed <a> elements appear there. The second
    # <noscript> block is one of them: its <img src="</noscript><a href="> swallows the first
    # anchor's opening, and the anchor that follows is a real link for a browser with
    # scripting off (checked against the reference HTML5 parser).
    assert info["links"] == [
        {"text": "Normal", "href": "https://normal.example.com/ok"},
        {"text": "y", "href": "https://noscript-q.example.com/p"},
    ]

    by_host = {h["host"]: h for h in info["link_hosts"]}
    assert "unparsed" not in by_host["normal.example.com"]
    assert by_host["noscript-q.example.com"] == {"host": "noscript-q.example.com", "count": 1}
    for hidden_host in (
        "comment.example.com",
        "noscript.example.com",
        "srcset-a.example.com",
        "srcset-b.example.com",
        "backtick.example.com",
        "unterminated.example.com",
    ):
        assert by_host[hidden_host] == {
            "host": hidden_host,
            "count": 1,
            "unparsed": True,
        }

    unparsed_warnings = [
        w for w in info["warnings"] if "outside normal markup" in w
    ]
    assert len(unparsed_warnings) == 1
    assert "backtick.example.com" in unparsed_warnings[0]
    assert "(+1 more)" in unparsed_warnings[0]


# ---------------------------------------------------------------------------
# 8. Round 5: parser-independent planted host scan (5,200 cases < 5 s) and
#    deceptive link text / self-closing <a> / nosemi charrefs / numeric hosts
# ---------------------------------------------------------------------------

_PLANT_WRAPPERS: tuple[tuple[str, str, str], ...] = (
    (
        "cdata_after_breakout",
        '<svg><p><![CDATA[><a href="{U}">https://good.example/login</a>]]></p>',
        "full",
    ),
    (
        "cdata_after_breakout_backtick",
        '<svg><p><![CDATA[><a data=`1 href="{U}">https://good.example/login</a>]]></p>',
        "full",
    ),
    (
        "cdata_after_breakout_backtick2",
        '<svg><p><![CDATA[><a data=`1 href={U} class=`>https://good.example/login</a>]]></p>',
        "full",
    ),
    (
        "select_xmp",
        '<select><xmp></select><a href="{U}">https://good.example/login</a></xmp>',
        "full",
    ),
    (
        "select_xmp_backtick",
        '<select><xmp></select><a data=`1 href="{U}">https://good.example/login</a></xmp>',
        "full",
    ),
    (
        "select_style",
        '<select><style></select><a href="{U}">https://good.example/login</a></style>',
        "full",
    ),
    (
        "select_plaintext",
        '<select><plaintext></select><a href="{U}">https://good.example/login</a>',
        "full",
    ),
    (
        "select_svg_cdata",
        '<select><svg></select><![CDATA[><a href="{U}">https://good.example/login</a>]]>',
        "full",
    ),
    (
        "base_slash_backslash",
        '<base href="https://good.example/"><a href="{U}">good.example</a>',
        "full",
    ),
    (
        "base_backslash_backslash",
        '<base href="https://good.example/"><a href="{U}">good.example</a>',
        "full",
    ),
    ("nobase_slash_backslash", '<a href="{U}">Sign in</a>', "full"),
    ("unc_href", '<a href="{U}">Quarterly report</a>', "full"),
    ("ftp_single_slash", '<a href="{U}">Download</a>', "full"),
    (
        "css_url_inline",
        '<div style="background-image:url({U})">hello</div>',
        "full",
    ),
    ("css_import", "<style>@import url({U});</style><p>hello</p>", "full"),
    (
        "meta_refresh",
        '<meta http-equiv="refresh" content="0;url={U}"><p>hello</p>',
        "full",
    ),
    (
        "a_ping",
        '<a href="https://good.example/" ping="{U}">good.example</a>',
        "full",
    ),
    (
        "word_field",
        "<p><!--[if supportFields]><span style='mso-element:field-begin'></span> "
        'HYPERLINK "{U}" <span style=\'mso-element:field-separator\'></span>'
        "<![endif]-->https://good.example/login<!--[if supportFields]>"
        "<span style='mso-element:field-end'></span><![endif]--></p>",
        "full",
    ),
    (
        "vml_o_href",
        '<p>x</p><!--[if gte vml 1]><v:shape><v:imagedata o:href="{U}"/>'
        "</v:shape><![endif]-->",
        "full",
    ),
    (
        "mso_cond_backtick",
        '<p>x</p><!--[if mso]><a data=`1 href="{U}">'
        "https://good.example/login</a><![endif]-->",
        "full",
    ),
    (
        "iframe_srcdoc",
        '<iframe srcdoc="&lt;a href=&quot;{U}&quot;&gt;good.example&lt;/a&gt;">'
        "</iframe><p>x</p>",
        "srcdoc",
    ),
    (
        "imagesrcset",
        '<link rel="preload" as="image" imagesrcset="{U} 1x"><p>x</p>',
        "full",
    ),
    ("text_word_lf", "<p>Please log in at\n{U}</p>", "full"),
    ("text_word_tab", "<p>Please log in at\t{U}</p>", "full"),
    ("text_word_num10", "<p>Please log in at&#10;{U}</p>", "full"),
    ("text_word_num9", "<p>Please log in at&#9;{U}</p>", "full"),
)

_NAMED_FOR_CHAR: dict[str, str] = {
    ":": "colon",
    "/": "sol",
    "\\": "bsol",
    ".": "period",
    "@": "commat",
    "\t": "Tab",
    "\n": "NewLine",
}
_NOSEMI_DECOYS: tuple[str, ...] = (
    "good.example&sol@",
    "good.example&num@",
    "good.example&quest@",
    "good.example&bsol@",
)
_TEXT_WORD_SEPS: tuple[str, ...] = ("\n", "\t", "&#10;", "&#9;")


def _obfuscate_planted_url(
    raw_url: str,
    rng: random.Random,
    *,
    max_layers: int,
    browser_valid: bool = False,
) -> str:
    chars = list(raw_url)
    out: list[str] = []
    n = len(chars)
    for idx, ch in enumerate(chars):
        nxt = chars[idx + 1] if idx + 1 < n else ""
        r = rng.random()
        if r < 0.58:
            out.append(ch)
            continue
        code = ord(ch)
        if ch in _NAMED_FOR_CHAR and r < 0.72:
            ent_name = _NAMED_FOR_CHAR[ch]
            semi = ";" if (browser_valid or nxt.isalnum() or rng.random() < 0.5) else ""
            tok = f"&{ent_name}{semi}"
        elif r < 0.86:
            semi = ";" if (nxt.isdigit() or rng.random() < 0.45) else ""
            tok = f"&#{code}{semi}"
        else:
            hex_part = f"{code:x}" if rng.random() < 0.5 else f"{code:X}"
            is_hex_next = nxt in "0123456789abcdefABCDEF"
            semi = ";" if (is_hex_next or rng.random() < 0.45) else ""
            tok = f"&#x{hex_part}{semi}"

        if max_layers >= 2 and rng.random() < 0.25:
            amp_form = (
                "&amp;"
                if (browser_valid or tok[1:].isalnum() or rng.random() < 0.6)
                else "&amp"
            )
            tok = amp_form + tok[1:]
        out.append(tok)
    return "".join(out)


def _build_planted_url(idx: int, rng: random.Random, *, mode: str) -> tuple[str, str]:
    host = f"p{idx}.evil.example"
    # Encode host with optional percent-encoded dots, mixed case, tabs/newlines, trailing dots.
    host_parts = host.split(".")
    sep_choices = [".", ".", "%2e", "%2E"]
    encoded_labels: list[str] = []
    for lb in host_parts:
        chars: list[str] = []
        for ch in lb:
            c = ch.upper() if rng.random() < 0.35 else ch
            chars.append(c)
            if rng.random() < 0.12:
                chars.append(rng.choice(("\t", "\n", "\r")))
        encoded_labels.append("".join(chars))
    raw_host = encoded_labels[0]
    for lb in encoded_labels[1:]:
        raw_host += rng.choice(sep_choices) + lb
    if rng.random() < 0.25:
        raw_host += "." * rng.randint(1, 2)

    max_layers = 1 if mode == "srcdoc" else 2
    kind_r = rng.random()
    if kind_r < 0.15:
        # mailto: form
        if rng.random() < 0.5:
            url = f"mailto:user@{raw_host}"
        else:
            sep = rng.choice((",", ";", "%2C"))
            url = f"mailto:first@good.example{sep}target@{raw_host}?subject=hi"
        obfuscated = _obfuscate_planted_url(url, rng, max_layers=max_layers)
    else:
        nosemi_decoy = ""
        userinfo = ""
        ui_r = rng.random()
        if ui_r < 0.28:
            nosemi_decoy = rng.choice(_NOSEMI_DECOYS)
        elif ui_r < 0.56:
            userinfo = rng.choice(("portal.example.com@", "user:pass@"))
        port = f":{rng.choice((80, 443, 8080, 8443))}" if rng.random() < 0.35 else ""
        path = rng.choice(("/login", "\\share\\doc", "/a?x=1", ""))
        if kind_r < 0.40:
            # Scheme-relative (2+ '/' or '\')
            slashes = "".join(rng.choice(("/", "\\")) for _ in range(rng.randint(2, 4)))
            prefix = slashes
        else:
            scheme = rng.choice(("http", "https", "ftp", "ftps", "ws", "wss", "file"))
            mixed_scheme = "".join(
                (c.upper() if rng.random() < 0.4 else c)
                + (rng.choice(("\t", "\n", "\r")) if rng.random() < 0.12 else "")
                for c in scheme
            )
            slashes = "".join(rng.choice(("/", "\\")) for _ in range(rng.randint(0, 3)))
            prefix = f"{mixed_scheme}:{slashes}"

        if nosemi_decoy:
            obf_prefix = _obfuscate_planted_url(
                prefix, rng, max_layers=max_layers, browser_valid=True
            )
            obf_tail = _obfuscate_planted_url(
                f"{raw_host}{port}{path}",
                rng,
                max_layers=max_layers,
                browser_valid=True,
            )
            obfuscated = f"{obf_prefix}{nosemi_decoy}{obf_tail}"
        else:
            url = f"{prefix}{userinfo}{raw_host}{port}{path}"
            obfuscated = _obfuscate_planted_url(url, rng, max_layers=max_layers)

    if mode == "srcdoc":
        obfuscated = obfuscated.replace("&", "&amp;")
    return host, obfuscated


def test_raw_host_scan_5000_planted_urls_across_wrappers():
    """The raw host scan computes the union of hosts across six readings of the
    raw text (three character-reference readings crossed with two whitespace
    readings); because the result is a set union, adding a reading can only add
    hosts and can never remove a host."""
    rng = random.Random(2026111605)
    total = 5200
    t0 = time.perf_counter()

    for idx in range(total):
        w_name, template, mode = _PLANT_WRAPPERS[idx % len(_PLANT_WRAPPERS)]
        expected_host, planted_url = _build_planted_url(idx, rng, mode=mode)
        doc = template.replace("{U}", planted_url)
        info = inspect_draft_links(doc)
        hosts = {h["host"] for h in info["link_hosts"]}
        assert expected_host in hosts or info.get("links_unverified"), (
            f"case {idx} ({w_name}) missed {expected_host!r} in {planted_url!r}: {info!r}"
        )

        # Also plant every generated URL in text position after a word + \n/\t/&#10;/&#9;.
        sep = _TEXT_WORD_SEPS[idx % len(_TEXT_WORD_SEPS)]
        text_doc = f"<p>Please review{sep}{planted_url}</p>"
        text_info = inspect_draft_links(text_doc)
        text_hosts = {h["host"] for h in text_info["link_hosts"]}
        assert expected_host in text_hosts or text_info.get("links_unverified"), (
            f"text case {idx} (sep={sep!r}) missed {expected_host!r} "
            f"in {planted_url!r}: {text_info!r}"
        )

    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0 * TIME_MARGIN, f"planted host scan took {elapsed:.2f}s (expected < 5s)"


def test_round5_mixed_script_dot_spaces_selfclosing_anchor_and_nosemi_charrefs():
    # 1. Mixed ASCII-letter + non-ASCII link text tokens warn with U+XXXX code points.
    for cp_hex, deceptive_text in (
        ("U+2027", "good\u2027example\u2027com"),
        ("U+A4F8", "good\ua4f8example\ua4f8com"),
        ("U+0660", "good\u0660example\u0660com"),
    ):
        doc = f'<p><a href="https://evil.example/login">{deceptive_text}</a></p>'
        san = sanitise_html(doc)
        info = inspect_draft_links(san.html)
        assert any(
            "link mismatch" in w and cp_hex in w for w in info["warnings"]
        ), (cp_hex, info)

    # Pure non-Latin (Arabic) link text gets NO warning.
    arabic_ok = sanitise_html(
        '<p dir="rtl"><a href="https://portal.example.com/ar">البوابة الإلكترونية للخدمات</a></p>'
    )
    arabic_info = inspect_draft_links(arabic_ok.html)
    assert arabic_info["warnings"] == []

    # 2. Whitespace around dots (hair spaces and text split across table cells).
    for label, raw_doc in (
        (
            "hair_spaces",
            '<p><a href="https://evil.example/login">good\u200a.\u200aexample\u200a.\u200acom</a></p>',
        ),
        (
            "td_split_div_b",
            '<div><a href="https://evil.example/login"><b>'
            '<table cellpadding="0" cellspacing="0"><tr><td>good.</td><td>example</td><td>.com</td></tr></table>'
            "</b></a></div>",
        ),
    ):
        san_d = sanitise_html(raw_doc)
        info_d = inspect_draft_links(san_d.html)
        assert any(
            "link mismatch" in w and "good.example.com" in w and "evil.example" in w
            for w in info_d["warnings"]
        ), (label, info_d)

    # 3. <a href="..."/>text</a> ignores self-closing '/' on non-void <a>.
    sc_doc = '<p><a href="https://evil.example/login" title="Open portal"/>https://good.example/login</a></p>'
    sc_san = sanitise_html(sc_doc)
    assert (
        '<a href="https://evil.example/login" title="Open portal">https://good.example/login</a>'
        in sc_san.html
    )
    for target_doc in (sc_doc, sc_san.html):
        sc_info = inspect_draft_links(target_doc)
        assert sc_info["links"] == [
            {"text": "https://good.example/login", "href": "https://evil.example/login"}
        ]
        assert any(
            "link mismatch" in w and "good.example" in w and "evil.example" in w
            for w in sc_info["warnings"]
        )

    # 4. Numeric character references without ';' in attributes.
    for raw_nosemi, expected_host in (
        ('<p><a href="https://portal.example.com&#64evil.example/login">Sign in</a></p>', "evil.example"),
        ('<p><a href="https:&#47&#47evil.example/login">Sign in</a></p>', "evil.example"),
        ('<p><a href="https://portal.example.com&#46evil.example/login">Sign in</a></p>', "portal.example.com.evil.example"),
    ):
        ns_info = inspect_draft_links(raw_nosemi)
        assert any(h["host"] == expected_host for h in ns_info["link_hosts"]), (raw_nosemi, ns_info)

    # Sanitiser strips userinfo smuggled via &#64 without ';'.
    san_at = sanitise_html(
        '<p><a href="https://portal.example.com&#64evil.example/login">Sign in</a></p>'
    )
    assert "href=" not in san_at.html
    assert san_at.sanitised["removed_links"] == 1

    # 5. mailto: percent-encoded ',' / ';' / CRLF stripped in sanitise_html and surfaced in preview.
    for mail_doc in (
        '<p><a href="mailto:a@good.example%2Cb@evil.example">a@good.example</a></p>',
        '<p><a href="mailto:a@good.example;b@evil.example">a@good.example</a></p>',
        '<p><a href="mailto:a@good.example%0D%0ABcc:b@evil.example">a@good.example</a></p>',
    ):
        m_san = sanitise_html(mail_doc)
        assert 'href="mailto:a@good.example"' in m_san.html
        assert m_san.sanitised["removed_links"] == 1
        m_raw_info = inspect_draft_links(mail_doc)
        m_hosts = {h["host"] for h in m_raw_info["link_hosts"]}
        assert {"good.example", "evil.example"} <= m_hosts
        assert any("link mismatch" in w for w in m_raw_info["warnings"])

    # 6. Numeric host warnings (decimal int, hex int, dotted IPv4, IPv6) and >253-char host cut to 80.
    for num_url, expected_num_host in (
        ("https://3221225985/admin", "3221225985"),
        ("https://0xC0000201/admin", "0xc0000201"),
        ("https://192.0.2.1/admin", "192.0.2.1"),
        ("https://[::1]/admin", "[::1]"),
    ):
        num_info = inspect_draft_links(f'<p><a href="{num_url}">Open</a></p>')
        assert num_info["link_hosts"] == [{"host": expected_num_host, "count": 1}]
        assert any("numeric link host address" in w for w in num_info["warnings"])

    long_host = "good.example." + ".".join(["a" * 60] * 40) + ".evil.example"
    long_info = inspect_draft_links(f'<p><a href="https://{long_host}/login">Sign in</a></p>')
    assert len(long_info["link_hosts"]) == 1
    shown_long = long_info["link_hosts"][0]["host"]
    assert len(shown_long) == 80
    assert "evil.example" in shown_long
    assert any("overlong link host" in w for w in long_info["warnings"])


def test_round6_glued_entity_prefixes_and_mailto_mismatch_tier():
    # 1. Glued '&amp...;' entity prefixes decode across passes in the raw host scan.
    glued_doc = (
        "<!-- https&ampcolon;&ampsol;&ampsol;user&ampcommat;"
        "glued&ampTab;host&ampperiod;evil&ampperiod;example/path -->"
    )
    glued_info = inspect_draft_links(glued_doc)
    assert glued_info["link_hosts"] == [
        {"host": "glued", "count": 1, "unparsed": True},
        {"host": "gluedhost.evil.example", "count": 1, "unparsed": True},
    ]

    # 2. mailto: multi-recipient and query warnings sit in the mismatch tier:
    #    never folded (> 3 kept verbatim), never cut by benign floods, and the
    #    links and their hosts are ordered first among mismatching entries.
    benign_25 = "".join(
        f'<a href="https://benign-{i:02d}.example.com/">Doc {i}</a> '
        for i in range(25)
    )
    empties_25 = "".join(
        f'<a href="https://empty-{i:02d}.example.com/"></a>' for i in range(25)
    )
    mailto_links = "".join(
        f'<a href="mailto:billing@portal.example.com?cc=spy{i}@zzz-evil-{i}.example">'
        "billing@portal.example.com</a>"
        for i in range(5)
    ) + (
        '<a href="mailto:billing@portal.example.com,extra@zzz-multi.example">'
        "Contact billing</a>"
    )
    doc = f"<html><body><p>{benign_25}{empties_25}{mailto_links}</p></body></html>"
    info = inspect_draft_links(doc)

    # All 6 mailto links appear first in `links` ahead of the 25 benign links.
    assert len(info["links"]) == MAX_PREVIEW_LINKS
    assert all(item["href"].startswith("mailto:") for item in info["links"][:6])

    # All zzz-evil-* and zzz-multi hosts appear in the mismatching-host group at the top.
    top_hosts = {h["host"] for h in info["link_hosts"][:7]}
    assert "zzz-multi.example" in top_hosts
    for i in range(5):
        assert f"zzz-evil-{i}.example" in top_hosts

    # None of the 5 mailto query warnings or the multi-recipient warning were folded or cut.
    assert sum(1 for w in info["warnings"] if "mailto link carries query parameters:" in w) == 5
    assert sum(1 for w in info["warnings"] if "mailto link specifies multiple recipients:" in w) == 1


# ---------------------------------------------------------------------------
# 9. Round 7: permanent compiled-regex linearity stress, 32-row Promise 2
#    verification matrix, and singular notices preceding 250 mismatch warnings
# ---------------------------------------------------------------------------

_STRESS_CHARS: tuple[str, ...] = (
    "\xa0", " ", "\t", "\n", ".", "&", "#", ";", "/", "\\", "@", "a", "A", "1",
    "-", "_", ":", "%", "=", '"', "'", "`", "<", ">", "(", ")", ",",
    "x", "\u0301", "\u200b", "\u0627",
)
_STRESS_PAIRS: tuple[str, ...] = (
    " .", ". ", "a.", ".a", "a ", " a", "&#", "&a", "a&", "1.", "/\\",
    "a@", "@a", "a-", "-a", "a:", ":a", "%2", '="', "a\t", "\n ", " \n",
    "a\u0301", ". \t", "&amp", "&#x2", "x.y",
)
_STRESS_HEADS: tuple[str, ...] = ("", "a.")
_STRESS_ENDS: tuple[str, ...] = ("", "!")


def _regex_call_seconds(rx: re.Pattern[str], text: str) -> float:
    t0 = time.perf_counter()
    for _ in rx.finditer(text):
        pass
    rx.sub("", text)
    rx.search(text)
    rx.match(text)
    return time.perf_counter() - t0


def _find_superlinear_input(
    rx: re.Pattern[str],
    shapes: list[tuple[str, str, str]],
) -> tuple[float, float, str, str, str] | None:
    """Judge growth, not absolute time: an input is super-linear when quadrupling its length
    multiplies the time by much more than four. Cheap inputs are skipped after the first
    measurement, so a clean pattern costs one short pass."""
    for unit, head, end in shapes:
        small = head + unit * (4_000 // len(unit)) + end
        t_small = _regex_call_seconds(rx, small)
        if t_small < 0.002:
            continue
        big = head + unit * (16_000 // len(unit)) + end
        t_big = _regex_call_seconds(rx, big)
        if t_big > 0.02 and t_big / t_small > 9.0:
            return (t_small, t_big, head, unit, end)
    return None


def test_all_compiled_regexes_linear_under_20k_stress():
    """Feed every compiled regular expression in ewsmcp.htmlsafe runs of every character and
    pair from regex_stress.py across heads ('', 'a.') and tails ('', '!'); fail when the time
    of any input grows much faster than its length (4,000 against 16,000 characters). The
    same procedure must catch the old r'\\s*\\.\\s*', so the test cannot go blind."""
    patterns = [
        (name, val)
        for name, val in vars(htmlsafe_mod).items()
        if isinstance(val, re.Pattern)
    ]
    assert len(patterns) >= 24

    shapes = [
        (unit, head, end)
        for unit in (*_STRESS_CHARS, *_STRESS_PAIRS)
        for head in _STRESS_HEADS
        for end in _STRESS_ENDS
    ]

    for name, rx in patterns:
        hit = _find_superlinear_input(rx, shapes)
        assert hit is None, (
            f"{name} ({rx.pattern[:70]!r}) grows faster than its input: "
            f"{hit[0]:.3f}s at 4,000 chars, {hit[1]:.3f}s at 16,000 on "
            f"head={hit[2]!r} unit={hit[3]!r} end={hit[4]!r}"
        )

    # Self-check: the old quadratic pattern must be caught by the exact same procedure.
    old_quadratic = re.compile(r"\s*\.\s*")
    assert _find_superlinear_input(old_quadratic, shapes) is not None, (
        "regex stress self-check failed to catch r'\\s*\\.\\s*'"
    )


_P2_A300K = "a" * 300_000

P2_VERIFY_CASES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "T1 named ref without ; (no reader decodes it) text",
        "<p>http://good.example&sol@evil.example/x</p>",
        ("evil.example",),
    ),
    (
        "T2 same in style url()",
        '<p style="background:url(http://good.example&sol@evil.example/x.png)">hi</p>',
        ("evil.example",),
    ),
    (
        "T3 same in meta refresh",
        '<meta http-equiv="refresh" content="0;url=http://good.example&sol@evil.example/"><p>hi</p>',
        ("evil.example",),
    ),
    (
        "T4 double-encoded ref (reader decodes once)",
        '<p style="background:url(http://good.example&amp;sol;@evil.example/x.png)">hi</p>',
        ("evil.example",),
    ),
    (
        "T5 double-encoded numeric ref in text",
        "<p>http://good.example&amp;#47;@evil.example/x</p>",
        ("good.example&",),
    ),
    (
        "T6 &num without ;",
        "<p>http://good.example&num@evil.example/x</p>",
        ("evil.example",),
    ),
    (
        "T7 &quest without ;",
        "<p>http://good.example&quest@evil.example/x</p>",
        ("evil.example",),
    ),
    (
        "N1 letter + newline + URL in text",
        "<p>Please visit\nhttp://evil.example/login</p>",
        ("evil.example",),
    ),
    (
        "N2 letter + CRLF + URL in pre",
        "<pre>Portal\r\nhttps://evil.example/login</pre>",
        ("evil.example",),
    ),
    (
        "N3 letter + tab + URL",
        "<p>see\thttp://evil.example/</p>",
        ("evil.example",),
    ),
    (
        "N4 letter + &#10; + URL",
        "<p>see&#10;http://evil.example/</p>",
        ("evil.example",),
    ),
    (
        "N5 control: letter + space + URL",
        "<p>see http://evil.example/</p>",
        ("evil.example",),
    ),
    (
        "N6 letter + newline + mailto",
        "<p>write\nmailto:b@evil.example</p>",
        ("evil.example",),
    ),
    (
        "U1 NBSP in userinfo, style url()",
        '<p style="background:url(http://good.example\xa0@evil.example/x.png)">hi</p>',
        ("good.example",),
    ),
    (
        "U2 quote char in userinfo, style url('')",
        "<p style=\"background:url('http://good.example&quot;@evil.example/x.png')\">hi</p>",
        ("evil.example",),
    ),
    (
        "U3 ) in userinfo, quoted css url",
        "<p style=\"background:url('http://good.example)@evil.example/x.png')\">hi</p>",
        ("evil.example",),
    ),
    (
        "U4 apostrophe in userinfo, meta refresh",
        '<meta http-equiv="refresh" content="0;url=http://good.example\'@evil.example/"><p>hi</p>',
        ("evil.example",),
    ),
    (
        "U5 > in userinfo, quoted attr",
        '<p data-u="http://good.example>@evil.example/x">t</p>',
        ("evil.example",),
    ),
    (
        "U6 backtick in userinfo, text",
        "<p>http://good.example`@evil.example/x</p>",
        ("evil.example",),
    ),
    (
        "L1 300,000-char userinfo, style url()",
        '<p style="background:url(http://' + _P2_A300K + '@evil.example/x.png)">hi</p>',
        ("evil.example",),
    ),
    (
        "L2 300,000-char userinfo, text",
        "<p>http://" + _P2_A300K + "@evil.example/x</p>",
        ("evil.example",),
    ),
    (
        "P1 non-digit port",
        "<p>http://evil.example:80a/x</p>",
        ("evil.example",),
    ),
    (
        "S1 file scheme in text",
        "<p>file://evil.example/share/x</p>",
        ("evil.example",),
    ),
    (
        "S2 file scheme in style url()",
        '<p style="background:url(file://evil.example/share/x.png)">hi</p>',
        ("evil.example",),
    ),
    (
        "S3 UNC in style url()",
        '<p style="background:url(\\\\evil.example\\share\\x.png)">hi</p>',
        ("evil.example",),
    ),
    (
        "S4 ftps",
        "<p>ftps://evil.example/</p>",
        ("evil.example",),
    ),
    (
        "R1 meta refresh, scheme-relative after ;",
        '<meta http-equiv="refresh" content="0;//evil.example/"><p>hi</p>',
        ("evil.example",),
    ),
    (
        "R2 meta refresh, scheme-relative after ,",
        '<meta http-equiv="refresh" content="0,//evil.example/"><p>hi</p>',
        ("evil.example",),
    ),
    (
        "R3 scheme-relative after : in css",
        '<p style="background:url(x);background-image:image-set(//evil.example/x 1x)">t</p>',
        ("evil.example",),
    ),
    (
        "I1 sharp-s host next to its ss twin in a link",
        '<p><a href="http://fass.example/">fass.example</a></p>'
        '<p style="background:url(http://fa\u00df.example/x.png)">t</p>',
        ("fass.example", "fa\u00df.example"),
    ),
    (
        "I2 sharp-s host alone",
        '<p style="background:url(http://fa\u00df.example/x.png)">t</p>',
        ("fass.example", "fa\u00df.example"),
    ),
    (
        "Z1 NUL inside host (text)",
        "<p>http://ev\x00il.example/</p>",
        ("evil.example",),
    ),
]


@pytest.mark.parametrize(
    ("label", "body", "expected_hosts"),
    P2_VERIFY_CASES,
    ids=[c[0].split()[0] for c in P2_VERIFY_CASES],
)
def test_p2_verify_all_32_rows_surface_reader_hosts(
    label: str, body: str, expected_hosts: tuple[str, ...]
) -> None:
    res = inspect_draft_links(body)
    listed = {h["host"] for h in res.get("link_hosts", [])}
    written = {
        h["as_written"]
        for h in res.get("link_hosts", [])
        if h.get("as_written") is not None
    }
    surfaced = listed | written
    missing = [h for h in expected_hosts if h not in surfaced]
    assert not missing, f"{label}: missing {missing!r} in {res!r}"


def test_promise3_singular_notices_precede_250_mismatches_and_count_line():
    # 250 distinct mismatching links across 59 anchor hosts + 1 hidden host = 60 hosts total.
    parts = [
        f'<p><a href="https://h{i % 59:02d}.evil.example/{i}">good{i}.example</a></p>'
        for i in range(250)
    ]
    parts.append("<p>ref http://hidden-target.example/x</p>")
    raw_doc = "".join(parts)

    for tag, doc in (("foreign", raw_doc), ("sanitised", sanitise_html(raw_doc).html)):
        info = inspect_draft_links(doc)
        warnings = info["warnings"]
        assert info["links_unverified"] is True, (tag, info)
        assert info["link_hosts_total"] == 60, (tag, info)

        omission_idxs = [i for i, w in enumerate(warnings) if "omitted" in w]
        outside_idxs = [
            i for i, w in enumerate(warnings) if "outside normal markup" in w
        ]
        mismatch_idxs = [
            i for i, w in enumerate(warnings) if w.startswith("link mismatch:")
        ]
        count_idxs = [
            i
            for i, w in enumerate(warnings)
            if w == f"{250 - MAX_MISMATCH_WARNINGS} more mismatching link warning(s)"
        ]

        assert omission_idxs == [0], (tag, warnings[:5])
        assert outside_idxs == [1], (tag, warnings[:5])
        assert len(mismatch_idxs) == MAX_MISMATCH_WARNINGS, (tag, len(mismatch_idxs))
        assert max(omission_idxs[0], outside_idxs[0]) < min(mismatch_idxs), (
            tag,
            warnings[:5],
        )
        assert count_idxs == [2 + MAX_MISMATCH_WARNINGS], (tag, warnings[-3:])
        assert info["warnings_total"] == 252, (tag, info.get("warnings_total"))


def test_round8_glued_schemes_and_slashless_host_filter():
    for doc, expected in (
        (
            "<p>Please review\nmailto:user@p0.evil.exampl&#10&#69;</p>",
            "p0.evil.example",
        ),
        (
            "<p>Please review\nhttps:p0.evil.exampl&#10&#69;</p>",
            "p0.evil.example",
        ),
        (
            "<p>Please review\n//p0.evil.exampl&#10&#69;/login</p>",
            "p0.evil.example",
        ),
        ("<p>Please review\nhttps:[::1]</p>", "[::1]"),
        ("<p>Please review\nhttps:3221225985</p>", "3221225985"),
    ):
        info = inspect_draft_links(doc)
        hosts = {h["host"] for h in info["link_hosts"]}
        assert expected in hosts, (doc, info)

    noise = inspect_draft_links(
        '<p style="widows:2;orphans:2">https:notahost news:today profile:admin</p>'
    )
    assert noise["link_hosts"] == []


def test_round9_anchor_wrapping_table_and_fixed_point_stability():
    # 1a. Anchor wrapping a table with deceptive cell text is kept and warned.
    deceptive_btn = (
        '<a href="https://evil.example/login">'
        "<table><tr><td>portal.example.com</td><td>ok</td></tr></table></a>"
    )
    san_dec = sanitise_html(deceptive_btn)
    assert san_dec.html == f"<html><body>{deceptive_btn}</body></html>"
    assert san_dec.sanitised is None
    assert_allowlist_valid(san_dec.html)
    info_dec = inspect_draft_links(san_dec.html)
    assert info_dec["links"] == [
        {"text": "portal.example.com ok", "href": "https://evil.example/login"}
    ]
    assert any(
        "link mismatch" in w and "portal.example.com" in w and "evil.example" in w
        for w in info_dec["warnings"]
    )

    # 1b. Anchor wrapping a table with neutral cell text is kept without a warning.
    neutral_btn = (
        '<a href="https://portal.example.com/login">'
        "<table><tr><td>Sign in</td><td>ok</td></tr></table></a>"
    )
    san_neu = sanitise_html(neutral_btn)
    assert san_neu.html == f"<html><body>{neutral_btn}</body></html>"
    assert san_neu.sanitised is None
    assert_allowlist_valid(san_neu.html)
    info_neu = inspect_draft_links(san_neu.html)
    assert info_neu["links"] == [
        {"text": "Sign in ok", "href": "https://portal.example.com/login"}
    ]
    assert info_neu["warnings"] == []

    # 1c. Anchor wrapping an empty table is removed (and counted in removed_links).
    empty_tbl_btn = (
        "<p>Header</p>"
        '<a href="https://evil.example/login"><table><tr><td>   </td><td>&#8203;</td></tr></table></a>'
    )
    san_emp = sanitise_html(empty_tbl_btn)
    assert "href=" not in san_emp.html
    assert "<a" not in san_emp.html
    assert "<table><tr><td>   </td><td>\u200b</td></tr></table>" in san_emp.html
    assert san_emp.sanitised == {
        "removed_elements": [],
        "removed_attributes": 0,
        "removed_links": 1,
    }
    assert_allowlist_valid(san_emp.html)
    assert inspect_draft_links(san_emp.html)["links"] == []

    with pytest.raises(HTMLValidationError, match="no visible text"):
        sanitise_html('<a href="https://evil.example/login"><table><tr><td></td></tr></table></a>')

    # 2. Removable wrappers around blocks inside pre / ul / ol / p / td stabilise in one call,
    #    preserving first-pass removal counts.
    for raw_in, expected_out in (
        (
            '<pre><a href="https://x.example/"><ul></ul></a>t</pre>',
            "<html><body><pre></pre><ul></ul>t</body></html>",
        ),
        (
            '<ul><li>x</li><a href="https://x.example/"><ol><hr></ol></a></ul>',
            "<html><body><ul><li>x</li></ul><ol><hr></ol></body></html>",
        ),
        (
            '<ol><li>x</li><a href="https://x.example/"><ul><hr></ul></a></ol>',
            "<html><body><ol><li>x</li></ol><ul><hr></ul></body></html>",
        ),
    ):
        first = sanitise_html(raw_in)
        assert first.html == expected_out, (raw_in, first)
        assert first.sanitised == {
            "removed_elements": [],
            "removed_attributes": 0,
            "removed_links": 1,
        }
        assert_allowlist_valid(first.html)
        second = sanitise_html(first.html)
        assert second.html == first.html
        assert second.text == first.text
        assert second.sanitised is None




