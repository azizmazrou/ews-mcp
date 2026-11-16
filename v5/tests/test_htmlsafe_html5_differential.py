"""Permanent differential tests comparing ewsmcp.htmlsafe against html5lib
(the reference WHATWG HTML5 parser) over 6,500 seeded hostile inputs plus the
regression cases from missed_r3 and probe-table-r3.
"""

from __future__ import annotations

import inspect as py_inspect
import random
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import pytest

import ewsmcp.htmlsafe as htmlsafe_mod
from ewsmcp.htmlsafe import HTMLValidationError, inspect_draft_links, sanitise_html

html5lib = pytest.importorskip(
    "html5lib",
    reason="html5lib is required for HTML5 reference differential tests (install .[dev])",
)

from conftest import TIME_MARGIN  # noqa: E402


XHTML_NS = "http://www.w3.org/1999/xhtml"

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

# Active (namespace_kind, element, attribute) triples from the reference harness.
ACTIVE = {
    ("html", t, a)
    for t, a in (
        ("a", "href"),
        ("area", "href"),
        ("base", "href"),
        ("link", "href"),
        ("img", "src"),
        ("iframe", "src"),
        ("frame", "src"),
        ("embed", "src"),
        ("script", "src"),
        ("audio", "src"),
        ("video", "src"),
        ("video", "poster"),
        ("source", "src"),
        ("track", "src"),
        ("input", "src"),
        ("input", "formaction"),
        ("button", "formaction"),
        ("form", "action"),
        ("object", "data"),
        ("body", "background"),
        ("table", "background"),
        ("td", "background"),
        ("th", "background"),
    )
}
TAGS = (
    "a b i u p div span table tr td th ul li ol h1 pre code font blockquote br hr "
    "script style svg math iframe img form input button select option textarea "
    "noscript template title head body html xmp plaintext listing noembed noframes "
    "object embed area map base link meta foreignObject desc mtext annotation-xml "
    "mglyph malignmark frameset frame center nobr marquee details summary"
).split()
ATTRS = [
    'href="https://evil.example/x"',
    "href=https://evil.example/y",
    "href='https://evil.example/z'",
    'src="https://evil.example/i.png"',
    'onclick="x()"',
    'style="color:red"',
    'action="https://evil.example/f"',
    'xlink:href="https://evil.example/s"',
    'href="javascript:alert(1)"',
    'href="//evil.example/p"',
    'title="</a><a href=https://evil.example/t>"',
    "dir=rtl",
    'x="',
    "y='",
    'href="https://evil.example/q?a=1&amp;b=2"',
    'background="https://evil.example/b"',
    'formaction="https://evil.example/fa"',
    'data="https://evil.example/d"',
    'href="htt&#x70;s://evil.example/e"',
    'href="htt&#x70s://evil.example/en"',
    'href="https://portal.example.com&#64evil.example/at"',
    'href="https:&#47&#47evil.example/sl"',
    'href="https://portal.example.com&#46evil.example/dt"',
    'href="https://evil.example/&quot;"',
    "href=`https://evil.example/bt`",
    'data=`1 href="https://evil.example/btq"',
    "data=`1 href=https://evil.example/btu class=`",
    "data=`1",
    "class=`",
]
FRAGS = [
    "<!--", "-->", "<!-->", "<!--->", "--!>", "<![CDATA[", "]]>", "<![x]>",
    "<!DOCTYPE html>", "<?x ?>", "</", "<", ">", "&lt;", "&amp;", "&#60;",
    "&#60", "&#64", "&#47", "&amp", "`",
    "\x00", "\x0c", "\t", "\n", " ", "text", "portal.example.com", "'", '"',
    "=", "/", "</p>", "</a>", "</script>", "</style>", "</title>",
    "</textarea>", "</xmp>", "</noscript>", "</svg>", "</math>", "</table>",
    "</select>", "</template>", "<a href=https://evil.example/plain>click</a>",
    '<a data=`1 href="https://evil.example/fbt">click</a>',
    '<a href="https://portal.example.com&#64evil.example/fn">click</a>',
    "</iframe>", "</noembed>", "</noframes>", "</plaintext>",
    "<svg><a xlink:href='https://evil.example/svg'>s</a></svg>",
    "<math><mtext></table></mtext></math>",
]

_H5_PARSER = html5lib.HTMLParser(
    tree=html5lib.getTreeBuilder("etree"),
    namespaceHTMLElements=True,
)


def gen_hostile(rng: random.Random) -> str:
    out: list[str] = []
    for _ in range(rng.randint(1, 14)):
        r = rng.random()
        if r < 0.45:
            t = rng.choice(TAGS)
            a = " ".join(rng.choice(ATTRS) for _ in range(rng.randint(0, 2)))
            tail = rng.choice([">", ">", ">", "/>", "", " >"])
            out.append(f"<{t}{' ' if a else ''}{a}{tail}")
        elif r < 0.60:
            end = rng.choice([">", ">", " x>", ' " >', '/">', ""])
            out.append(f"</{rng.choice(TAGS)}{end}")
        else:
            out.append(rng.choice(FRAGS))
    return "".join(out)


def host_of(url: str) -> str | None:
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    return (u.hostname or "").lower() or None


def browser_links(raw: str) -> list[tuple[str, str, str, str]] | None:
    """[(kind, tag, attribute, host)] for every active URL html5lib's tree holds,
    or None if html5lib itself raises."""
    try:
        tree = _H5_PARSER.parse(raw)
    except Exception:  # noqa: BLE001
        return None
    out: list[tuple[str, str, str, str]] = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        ns, _, tag = el.tag[1:].partition("}")
        kind = "html" if ns.endswith("xhtml") else ("svg" if ns.endswith("svg") else "math")
        for k, v in el.attrib.items():
            name = k.split("}")[-1] if k.startswith("{") else k
            if (kind, tag, name) in ACTIVE or (
                kind == "svg" and tag in ("a", "image", "use", "script") and name == "href"
            ):
                h = host_of(v)
                if h:
                    out.append((kind, tag, name, h))
    return out


def preview_mentions_host(info: dict[str, Any], host: str) -> bool:
    if info.get("links_unverified"):
        return True
    for item in info.get("links", ()):
        if host in item.get("href", "").lower():
            return True
    for entry in info.get("link_hosts", ()):
        if host == entry.get("host") or host == entry.get("as_written"):
            return True
        if host in (entry.get("host") or "") or host in (entry.get("as_written") or ""):
            return True
    for w in info.get("warnings", ()):
        if host in w.lower():
            return True
    return False


def assert_html5lib_allowlist_clean(sanitised_doc: str) -> None:
    tree = _H5_PARSER.parse(sanitised_doc)
    for el in tree.iter():
        assert isinstance(el.tag, str), f"non-element node in html5lib tree: {el!r}"
        ns, _, tag = el.tag[1:].partition("}")
        assert ns == XHTML_NS, f"non-HTML namespace {ns!r} on <{tag}>"
        if tag == "html":
            assert not el.attrib, f"<html> attributes survived: {el.attrib!r}"
            continue
        if tag == "head":
            # html5lib always synthesizes an empty <head> element before <body>.
            assert not el.attrib and len(el) == 0 and not (el.text or "").strip(), (
                f"non-empty <head> in html5lib parse: attrib={el.attrib!r} len={len(el)}"
            )
            continue
        if tag == "body":
            allowed_attrs = SPEC_GLOBAL_ATTRS
        else:
            assert tag in SPEC_ELEMENTS, f"disallowed element <{tag}> in html5lib parse"
            allowed_attrs = SPEC_GLOBAL_ATTRS | SPEC_TAG_ATTRS.get(tag, frozenset())

        for raw_attr, val in el.attrib.items():
            attr = raw_attr.split("}")[-1] if raw_attr.startswith("{") else raw_attr
            assert not attr.startswith("on"), f"event handler {attr!r} on <{tag}>"
            assert attr in allowed_attrs, f"disallowed attr {attr!r} on <{tag}>"
            if attr == "href":
                compact = "".join(
                    ch for ch in val
                    if not ch.isspace() and unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
                )
                assert ":" in compact, f"href missing scheme: {val!r}"
                scheme = compact.split(":", 1)[0].lower()
                assert scheme in SPEC_SCHEMES, f"disallowed scheme {scheme!r} in {val!r}"
                parts = urlsplit(val)
                assert parts.scheme.lower() in SPEC_SCHEMES
                if parts.scheme.lower() in ("http", "https"):
                    assert parts.hostname and "@" not in parts.netloc
                else:
                    assert "?" not in val and "#" not in val


def test_runtime_htmlsafe_does_not_import_html5lib():
    assert "html5lib" not in py_inspect.getsource(htmlsafe_mod)


# ---------------------------------------------------------------------------
# Named regressions: missed_r3.json, probe-table-r3.txt MISSED rows, noscript
# ---------------------------------------------------------------------------

MISSED_R3_CASES: list[tuple[str, str, str]] = [
    ("button_formaction_after_slash_quote_endtag", '</h/"><button formaction=//m>', "m"),
    ("a_href_after_slash_quote_endtag", '</e/"><a href=//m>', "m"),
    (
        "svg_a_xlink_href_after_noembed_rawtext",
        '<noembed><m x="</noembed><svg><a xlink:href=//m>',
        "m",
    ),
    ("base_href_after_slash_quote_endtag", '</e/"><base href=//y>', "y"),
    ("script_src_quote_host_after_space_quote_endtag", '</t "><script src=//">', '"'),
    ("link_href_quote_host_after_slash_quote_endtag", '</e/"><link href=//">', '"'),
    ("input_formaction_quote_host_after_slash_quote_endtag", '</e/"><input formaction=//">', '"'),
    ("area_href_quote_host_after_slash_quote_endtag", '</e/"><area href=//">', '"'),
    ("img_src_after_slash_singlequote_endtag", "</d/'><img src=//p>", "p"),
]


@pytest.mark.parametrize(
    ("case_id", "raw_html", "expected_host"),
    MISSED_R3_CASES,
    ids=[c[0] for c in MISSED_R3_CASES],
)
def test_missed_r3_minimal_cases_all_surface_host(
    case_id: str, raw_html: str, expected_host: str
) -> None:
    links = browser_links(raw_html)
    assert links, case_id
    info = inspect_draft_links(raw_html)
    assert preview_mentions_host(info, expected_host), (case_id, info)
    for _, _, _, h in links:
        assert preview_mentions_host(info, h), (case_id, h, info)


PROBE_TABLE_MISSED_ROWS: list[tuple[str, str, str]] = [
    ("end_p_with_quote_before_link", '</p "><a href=//m>x</a>', "m"),
    (
        "noembed_rawtext_with_open_quote_before_link",
        '<noembed><m x="</noembed><a href=//m>x</a>',
        "m",
    ),
    (
        "noframes_rawtext_with_open_quote_before_link",
        '<noframes><m x="</noframes><a href=//m>x</a>',
        "m",
    ),
    ("xmp_rawtext_with_open_quote_before_link", '<xmp><m x="</xmp><a href=//m>x</a>', "m"),
    ("iframe_rawtext_with_open_quote_before_link", '<iframe><m x="</iframe><a href=//m>x</a>', "m"),
]


@pytest.mark.parametrize(
    ("row_id", "raw_html", "expected_host"),
    PROBE_TABLE_MISSED_ROWS,
    ids=[r[0] for r in PROBE_TABLE_MISSED_ROWS],
)
def test_probe_table_missed_rows_surface_host_and_sanitiser_keeps_text(
    row_id: str, raw_html: str, expected_host: str
) -> None:
    info = inspect_draft_links(raw_html)
    assert preview_mentions_host(info, expected_host), (row_id, info)

    res = sanitise_html(raw_html)
    assert "x" in res.text, (row_id, res)
    assert_html5lib_allowlist_clean(res.html)


def test_probe_table_noscript_row_surfaces_host_via_unparsed_backstop():
    raw_html = '<noscript><m x="</noscript><a href=//m>x</a>'
    info = inspect_draft_links(raw_html)
    assert info["links"] == []
    assert info["link_hosts"] == [{"host": "m", "count": 1, "unparsed": True}]
    assert any("outside normal markup" in w and "m" in w for w in info["warnings"])

    # Even when the unclosed quote inside <noscript> is itself on a URL attribute:
    raw_url_attr = '<noscript><img src="</noscript><a href="https://evil.example/p">x</a>'
    info_url_attr = inspect_draft_links(raw_url_attr)
    assert info_url_attr["links"] == []
    assert info_url_attr["link_hosts"] == [
        {"host": "evil.example", "count": 1, "unparsed": True}
    ]
    assert any(
        "outside normal markup" in w and "evil.example" in w
        for w in info_url_attr["warnings"]
    )


# ---------------------------------------------------------------------------
# Seeded 6,500-input differential check against html5lib (< 10 seconds)
# ---------------------------------------------------------------------------


def test_6500_seeded_hostile_inputs_against_html5lib():
    rng = random.Random(2026111604)
    total = 6500
    skipped = 0
    sanitised_accepted = 0
    t0 = time.perf_counter()

    for idx in range(total):
        raw = gen_hostile(rng)

        # Part A: every accepted sanitised output parsed by html5lib obeys the allow-list.
        try:
            res = sanitise_html(raw)
        except HTMLValidationError:
            res = None
        if res is not None:
            sanitised_accepted += 1
            assert_html5lib_allowlist_clean(res.html)

        # Part B: every active link host html5lib sees in raw is shown by the preview.
        links = browser_links(raw)
        if links is None:
            skipped += 1
            continue
        if links:
            info = inspect_draft_links(raw)
            for kind, tag, attr, host in links:
                assert preview_mentions_host(info, host), (
                    f"case {idx} missed {kind} <{tag} {attr}> host {host!r} "
                    f"on {raw!r}: {info!r}"
                )

    elapsed = time.perf_counter() - t0
    assert skipped <= total * 0.01, f"too many html5lib skips: {skipped}/{total}"
    assert sanitised_accepted >= 1500
    assert elapsed < 10.0 * TIME_MARGIN, f"differential suite took {elapsed:.2f}s (expected < 10s)"
