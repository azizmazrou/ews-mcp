"""MANDATORY gate B: Arabic search correctness across orthographic variants.

Seeded-mirror queries must match regardless of Arabic orthography:
- a body containing "تمت الإحاطة" is found by searching "الاحاطه"
  (bare-alef variant + teh-marbuta written as heh) and by the
  undiacritized form of any diacritized word;
- mixed Arabic/English queries work;
- Arabic-Indic digit queries match ASCII digits and vice versa.

Business-neutral synthetic text; example.com senders only. The SAME
normalize_ar() runs at index time and query time — that symmetry IS the
guarantee, and this suite is the gate on it.
"""

import asyncio
import time

import pytest

from conftest import make_settings

from ewsmcp.audit import AuditLog
from ewsmcp.cache.store import CacheStore
from ewsmcp.ids import get_aliaser
from ewsmcp.normalize import normalize_ar
from ewsmcp.tools import build_registry
from ewsmcp.tools.base import Context, dispatch

from test_cache_store import make_row


class NoTouchGateway:
    async def call(self, fn):
        raise AssertionError("EWS contacted — Arabic search must be local")


@pytest.fixture
def ctx(tmp_path):
    store = CacheStore(tmp_path / "mirror.db")
    now = int(time.time())
    store.upsert_messages([
        # diacritized + hamza-carrier forms in the body
        make_row("AR-1", subject="محضر الاجتماع",
                 body="تَمَّت الإِحاطَة بما ورد في الخِطاب وسنوافيكم بالرد.",
                 sender_email="pm@example.com", date_ts=now - 400),
        # teh marbuta + alef maqsura forms
        make_row("AR-2", subject="الموافقة على المستوى المطلوب",
                 body="نرجو مراجعة المسودة قبل الاجتماع القادم.",
                 sender_email="lead@example.com", date_ts=now - 300),
        # mixed Arabic/English + Arabic-Indic digits
        make_row("AR-3", subject="Budget تقرير الربع",
                 body="مرفق تقرير budget للربع الثالث لعام ٢٠٢٦.",
                 sender_email="analyst@example.com", date_ts=now - 200),
        # English-only control
        make_row("EN-1", subject="Weekly sync",
                 body="Minutes attached for the weekly sync.",
                 sender_email="ops@example.com", date_ts=now - 100),
    ])
    store.set_sync_state("item:inbox", "TOK", now)
    context = Context(
        settings=make_settings(),
        gateway=NoTouchGateway(),
        manager=None,
        aliaser=get_aliaser(str(tmp_path / "alias")),
        audit=AuditLog(str(tmp_path / "audit")),
        cache=store,
    )
    build_registry(context)
    return context


def _search(ctx, query):
    res = asyncio.run(dispatch(ctx, ctx.registry["search_messages"],
                               {"query": query}))
    assert res["ok"] is True, res
    assert res["source"] == "cache"
    return [item["id"] for item in res["items"]], res


def _ids_for(ctx, ews_ids):
    return {ctx.aliaser.alias_for(e, "m") for e in ews_ids}


def test_alef_hamza_and_teh_marbuta_variant_matches(ctx):
    """THE gate-B example: body says الإحاطة, query says الاحاطه."""
    ids, _ = _search(ctx, "الاحاطه")
    assert ids == list(_ids_for(ctx, ["AR-1"]))


def test_undiacritized_query_finds_diacritized_body(ctx):
    ids, _ = _search(ctx, "تمت")  # body has تَمَّت
    assert ids == list(_ids_for(ctx, ["AR-1"]))


def test_diacritized_query_finds_the_same_message(ctx):
    ids, _ = _search(ctx, "الإِحاطَة")
    assert ids == list(_ids_for(ctx, ["AR-1"]))


def test_hamza_carrier_and_maqsura_folds(ctx):
    # مستوى written with ي instead of ى must still match
    ids, _ = _search(ctx, "المستوي")
    assert ids == list(_ids_for(ctx, ["AR-2"]))


def test_mixed_arabic_english_query(ctx):
    ids, _ = _search(ctx, "budget تقرير")
    assert ids == list(_ids_for(ctx, ["AR-3"]))


def test_arabic_indic_digit_query(ctx):
    # body says ٢٠٢٦ (Arabic-Indic); both digit systems must hit
    ids, _ = _search(ctx, "2026")
    assert ids == list(_ids_for(ctx, ["AR-3"]))
    ids, _ = _search(ctx, "٢٠٢٦")
    assert ids == list(_ids_for(ctx, ["AR-3"]))


def test_english_control_still_works(ctx):
    ids, _ = _search(ctx, "weekly sync")
    assert ids == list(_ids_for(ctx, ["EN-1"]))


def test_every_fold_pair_explicitly():
    """Unit-level pin for each documented fold (the normalize_ar contract)."""
    pairs = [
        ("أعمال", "اعمال"),      # alef hamza above
        ("إدارة", "ادارة"),      # alef hamza below
        ("آفاق", "افاق"),        # alef madda
        ("ٱقرأ", "اقرا"),        # alef wasla (+ hamza-above fold)
        ("مبنى", "مبني"),        # alef maqsura → yeh
        ("خطة", "خطه"),          # teh marbuta → heh
        ("مؤسسة", "موسسه"),      # hamza on waw (+ teh marbuta)
        ("مسؤول", "مسوول"),      # hamza on waw mid-word
        ("رئيس", "رييس"),        # hamza on yeh
        ("مُدِير", "مدير"),      # diacritics stripped
        ("عـمـل", "عمل"),        # tatweel stripped
        ("١٢٣", "123"),          # Arabic-Indic digits
        ("۴۵۶", "456"),          # extended Arabic-Indic digits
    ]
    for variant, canonical in pairs:
        assert normalize_ar(variant) == normalize_ar(canonical), (
            f"fold failed: {variant!r} != {canonical!r}")
