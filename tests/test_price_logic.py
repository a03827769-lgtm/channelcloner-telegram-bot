"""Price extraction and offer/demand classification of the production story service (no copies of the
implementation: the service itself is exercised)."""
import pytest

from services.story_cloner_service import UZS_PER_USD, StoryClonerService


@pytest.mark.parametrize("text, expected", [
    ("750$", 750.0),
    ("$800", 800.0),
    ("1.500$", 1500.0),
    ("1,500$", 1500.0),
    ("65.000$", 65000.0),
    ("65,000 $", 65000.0),
    ("Narxi: 700 $", 700.0),
    ("Цена: 1200 у.е.", 1200.0),
    ("1 000 USD / oy", 1000.0),
    ("700$ dan boshlanadi", 700.0),
    ("Arenda: 77 kv.m kvartira, narxi 800$", 800.0),
    ("Narxi: 4 000 000 so'm", 4_000_000 / UZS_PER_USD),
    ("Yunusobod 4-mavze, 2 xona, 4-qavat, tel: +998901234567. Narxi: 750$", 750.0),
    ("2 xona evroremont, barcha qulayliklar bor", None),
])
def test_price_logic(text, expected):
    result = StoryClonerService.extract_price(text)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("text", [
    "Menga 2 xonali kvartira kerak, budjet 800$",
    "Ищу квартиру в Юнусабаде до 1000$",
    "Клиент бор, 3 хона керак, 1200$ гача",
    "Arenda kerak zudlik bilan",
    "Oilaga kvartira kerak",
    "Kvartira qidiryapman Yunusoboddan",
    "Сниму квартиру для семьи, 900$",
    "Ищем 2-комнатную возле метро",
])
def test_demand_detection(text):
    assert StoryClonerService.is_demand_post(text) is True


@pytest.mark.parametrize("text", [
    "Yunusobod 2 xona arendaga beriladi. Faqat oila kerak. Narxi: 800$",
    "Kvartira topshiriladi. Kvartirant kerak. Narxi: 750$",
    "Сдается 2-комнатная квартира. Нужна порядочная семья. 900$",
    "Sotiladi: 3 xona novostroyka, 70000$",
    "Yunusobod 6 mavze, 2 xona arendaga beriladi, 750$",
    "Yangi remont qilingan uy ijaraga beriladi",
])
def test_offer_detection(text):
    assert StoryClonerService.is_demand_post(text) is False
