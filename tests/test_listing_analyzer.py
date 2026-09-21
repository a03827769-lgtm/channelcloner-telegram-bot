# -*- coding: utf-8 -*-
import pytest
from services.listing_analyzer import listing_analyzer, ListingAnalyzer


def test_tashkent_districts_detection():
    cases = [
        ("Mirobod tumani, Oybek metrosi yaqinida 3 xonali kvartira beriladi", "Mirobod"),
        ("Сдается 2-комнатная квартира, Мирабадский район, Госпитальный", "Mirobod"),
        ("Юнусабад 4-квартал, отличная 2-х комнатная новостройка", "Yunusobod"),
        ("Chilonzor 9-kvartal, 1 xonali toza uy ijaraga beriladi", "Chilonzor"),
        ("Чиланзар Торговый центр, евроремонт 3 хона", "Chilonzor"),
        ("Mirzo Ulug'bek tumani, Maksim Gorkiy yaqinida", "Mirzo Ulug'bek"),
        ("Новомосковская, Дархан, элитный жилой комплекс", "Mirzo Ulug'bek"),
        ("Яккасарайский район, Шота Руставели, 4-комнатная", "Yakkasaroy"),
        ("Shayxontohur tumani, Labzak, Chorsu yaqinida", "Shayxontohur"),
        ("Сергели 7, Янги Сергели метро бекати ёнида", "Sergeli"),
        ("Yashnobod tumani, Parkentskiy bozor yaqinida", "Yashnobod"),
        ("Олмазор тумани, Себзар, 2 хонали шинам уй", "Olmazor"),
        ("Учтепа тумани, Урикзор бозори атрофида", "Uchtepa")
    ]
    for text, expected in cases:
        detected = listing_analyzer.extract_district(text)
        assert detected == expected, f"Failed for '{text}': got {detected}, expected {expected}"


def test_room_extraction():
    cases = [
        ("3 xonali kvartira beriladi", 3),
        ("2-xona, toza remont", 2),
        ("Сдается 4-комнатная квартира", 4),
        ("Шинам 1 хонали квартира", 1),
        ("3-х комнатная квартира люкс", 3),
        ("Студия с ремонтом в центре", 1),
        ("uch xonali shinam xonadon", 3),
        ("двухкомнатная новостройка", 2)
    ]
    for text, expected in cases:
        assert listing_analyzer.extract_rooms(text) == expected, f"Failed room extraction for '{text}'"


def test_area_extraction():
    cases = [
        ("Maydoni 85 kv.m, hamma sharoit bor", 85.0),
        ("Общая площадь 120 м2, 4 комнаты", 120.0),
        ("65 кв.м, новостройка с мебелью", 65.0),
        ("Katta zal, 105 m2, 3 xonali", 105.0)
    ]
    for text, expected in cases:
        assert listing_analyzer.extract_area(text) == expected, f"Failed area extraction for '{text}'"


def test_floor_extraction():
    cases = [
        ("4/9 etaj, lift ishlaydi", (4, 9)),
        ("7/16 этаж, прекрасный вид на город", (7, 16)),
        ("3-etaj, g'ishtli dom", (3, None)),
        ("5 этаж, дом с охраной", (5, None)),
        ("этаж 5, новостройка", (5, None)),
        ("qavat: 8, panelli uy", (8, None))
    ]
    for text, expected in cases:
        fl, tot = listing_analyzer.extract_floor(text)
        assert (fl, tot) == expected, f"Failed floor extraction for '{text}'"


def test_quality_score_and_badges():
    sample_text = """
    Mirobod tumani, Oybek metrosi yaqinida
    3 xonali premium kvartira ijaraga beriladi!
    Maydoni: 95 kv.m.
    Qavati: 5/12 etaj.
    Mualliflik dizayni asosida lyuks remont qilingan.
    Narxi: 1500$
    """
    meta = listing_analyzer.analyze(sample_text, photo_count=6, existing_price=1500.0)

    assert meta.district == "Mirobod"
    assert meta.rooms == 3
    assert meta.area == 95.0
    assert meta.floor == 5
    assert meta.total_floors == 12
    assert meta.is_luxury is True
    assert meta.quality_score >= 85
    assert "💎 PREMYUM" in meta.smart_badges
    assert "📍 Mirobod" in meta.smart_badges
    assert "🚪 3-xona" in meta.smart_badges
    assert "📐 95 m²" in meta.smart_badges

    # Automatic price extraction test when existing_price is omitted
    meta_auto = listing_analyzer.analyze(sample_text, photo_count=6)
    assert meta_auto.price == 1500.0
    assert meta_auto.quality_score >= 85
    assert "💎 PREMYUM" in meta_auto.smart_badges


def test_fingerprint_deduplication():
    # Two listings from different channels with different contacts and minor wording changes
    post_a = """
    Mirobod tumani, Oybek metrosi
    3 xonali lyuks kvartira beriladi.
    95 kv.m, 5-etaj.
    Narxi: 1500$
    Aloqa uchun: +998901234567 @agent_ali
    """
    post_b = """
    Мирабад, Ойбек!
    Сдается 3-х комнатная люкс квартира.
    95 м2, этаж 5.
    Цена: 1500 USD
    Контакт: +998998887766 @broker_tashkent t.me/broker
    """
    meta_a = listing_analyzer.analyze(post_a, photo_count=4, existing_price=1500.0)
    meta_b = listing_analyzer.analyze(post_b, photo_count=4, existing_price=1500.0)

    # Core parameters match: district Mirobod, 3 rooms, 95 sqm, $1500, floor 5
    assert meta_a.district == meta_b.district
    assert meta_a.rooms == meta_b.rooms
    assert meta_a.area == meta_b.area
    assert meta_a.floor == meta_b.floor
    assert meta_a.is_luxury == meta_b.is_luxury
