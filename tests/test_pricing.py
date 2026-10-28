"""
Тесты модуля services/pricing.py — стартовые цены и коэффициент срочности.
Цены синхронизированы с лендингом (раздел «Из чего складывается цена»).
"""
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta

from services.pricing import (
    calculate_starting_price,
    calculate_urgency_multiplier,
    _parse_deadline_to_hours,
)


# ─── Курсовая: 100 ₽/стр., min 2000 ₽ ────────────────────────────────────────

class TestKursovaya:
    def test_minimum_20_pages(self):
        price, unit, _ = calculate_starting_price("kursovaya", 20)
        assert price == 2000
        assert "стр" in unit

    def test_less_than_min_still_2000(self):
        price, _, _ = calculate_starting_price("kursovaya", 15)
        assert price == 2000

    def test_above_min_proportional(self):
        price, _, _ = calculate_starting_price("kursovaya", 30)
        assert price == 3000  # 2000 + 10*100

    def test_50_pages(self):
        price, _, _ = calculate_starting_price("kursovaya", 50)
        assert price == 5000  # 2000 + 30*100


# ─── Реферат: 30 ₽/стр., min 300 ₽ ───────────────────────────────────────────

class TestReferat:
    def test_minimum_10_pages(self):
        price, _, _ = calculate_starting_price("referat", 10)
        assert price == 300

    def test_less_than_min(self):
        price, _, _ = calculate_starting_price("referat", 5)
        assert price == 300

    def test_20_pages(self):
        price, _, _ = calculate_starting_price("referat", 20)
        assert price == 600  # 300 + 10*30


# ─── Статья: 200 ₽/стр., min 1200 ₽ ──────────────────────────────────────────

class TestStatya:
    def test_minimum_6_pages(self):
        price, _, _ = calculate_starting_price("statya", 6)
        assert price == 1200

    def test_8_pages(self):
        price, _, _ = calculate_starting_price("statya", 8)
        assert price == 1600  # 1200 + 2*200

    def test_less_than_min(self):
        price, _, _ = calculate_starting_price("statya", 3)
        assert price == 1200


# ─── Конспект: 20 ₽/стр., min 200 ₽ ──────────────────────────────────────────

class TestKonspekt:
    def test_minimum_10_pages(self):
        price, _, _ = calculate_starting_price("konspekt", 10)
        assert price == 200

    def test_less_than_min(self):
        price, _, _ = calculate_starting_price("konspekt", 5)
        assert price == 200

    def test_30_pages(self):
        price, _, _ = calculate_starting_price("konspekt", 30)
        assert price == 600  # 200 + 20*20


# ─── Домашка: фикс 300 ₽ за работу ───────────────────────────────────────────

class TestDomashka:
    def test_fixed_price(self):
        price, unit, _ = calculate_starting_price("domashka", 1)
        assert price == 300
        assert "работа" in unit

    def test_pages_ignored(self):
        price, _, _ = calculate_starting_price("domashka", 10)
        assert price == 300


# ─── Презентация: 45 ₽/слайд, min 450 ₽ ──────────────────────────────────────

class TestPrezentaciya:
    def test_minimum_10_slides(self):
        price, _, _ = calculate_starting_price("prezentaciya", 10)
        assert price == 450

    def test_15_slides(self):
        price, _, _ = calculate_starting_price("prezentaciya", 15)
        assert price == 675  # 450 + 5*45

    def test_30_slides(self):
        price, _, _ = calculate_starting_price("prezentaciya", 30)
        assert price == 1350  # 450 + 20*45

    def test_50_slides(self):
        price, _, _ = calculate_starting_price("prezentaciya", 50)
        assert price == 2250  # 450 + 40*45

    def test_unit_label(self):
        _, unit, _ = calculate_starting_price("prezentaciya", 12)
        assert "слайд" in unit


# ─── Другое (catch-all) ──────────────────────────────────────────────────────

class TestDrugoe:
    def test_minimum(self):
        price, _, _ = calculate_starting_price("drugoe", 5)
        assert price == 500

    def test_10_pages(self):
        price, _, _ = calculate_starting_price("drugoe", 10)
        assert price == 1000  # 500 + 5*100


# ─── Edge cases ──────────────────────────────────────────────────────────────

class TestEdge:
    def test_none_work_type(self):
        price, _, _ = calculate_starting_price(None, 5)
        assert price == 500  # falls back to "drugoe"

    def test_zero_pages(self):
        price, _, _ = calculate_starting_price("kursovaya", 0)
        assert price == 2000  # min applied

    def test_unknown_work_type_falls_to_drugoe(self):
        price, _, _ = calculate_starting_price("alien_type", 5)
        assert price == 500


# ─── Парсер дедлайна ─────────────────────────────────────────────────────────

class TestUrgencyParser:
    """Парсер _parse_deadline_to_hours — возвращает часы до дедлайна."""

    def test_срочно(self):
        assert _parse_deadline_to_hours("срочно") == 4.0

    def test_asap(self):
        assert _parse_deadline_to_hours("ASAP пожалуйста") == 4.0

    def test_немедленно(self):
        assert _parse_deadline_to_hours("нужно немедленно") == 4.0

    def test_сегодня(self):
        assert _parse_deadline_to_hours("сегодня") == 8.0

    def test_к_утру(self):
        assert _parse_deadline_to_hours("к утру") == 12.0

    def test_завтра(self):
        assert _parse_deadline_to_hours("завтра") == 24.0

    def test_к_завтра(self):
        assert _parse_deadline_to_hours("к завтра") == 24.0

    def test_послезавтра(self):
        assert _parse_deadline_to_hours("послезавтра") == 48.0

    def test_через_3_дня(self):
        assert _parse_deadline_to_hours("через 3 дня") == 72.0

    def test_5_дней(self):
        assert _parse_deadline_to_hours("5 дней") == 120.0

    def test_1_неделя(self):
        assert _parse_deadline_to_hours("1 неделя") == 168.0

    def test_2_часа(self):
        assert _parse_deadline_to_hours("через 2 часа") == 2.0

    def test_empty_returns_none(self):
        assert _parse_deadline_to_hours("") is None

    def test_garbage_returns_none(self):
        assert _parse_deadline_to_hours("abrakadabra без чисел") is None

    def test_date_dot_format_future(self):
        """Дата в формате DD.MM.YYYY в будущем → положительное число часов."""
        future = datetime.now() + timedelta(days=5)
        ds = future.strftime("%d.%m.%Y")
        hours = _parse_deadline_to_hours(ds)
        assert hours is not None
        assert 95 < hours < 145  # около 5 дней (± 24 ч на округление до 23:59)

    def test_15_января_словами(self):
        """«15 января» — парсит дату по слову месяца."""
        # Тест-агностичен по году
        result = _parse_deadline_to_hours("15 января")
        assert result is not None
        assert result > 0


# ─── Коэффициент срочности ───────────────────────────────────────────────────

class TestUrgencyMultiplier:
    def test_срочно_15(self):
        mul, label = calculate_urgency_multiplier("срочно")
        assert mul == 1.5
        assert "срочн" in label.lower()

    def test_сегодня_12(self):
        # 8 часов → < 12 → ×1.2
        mul, _ = calculate_urgency_multiplier("сегодня")
        assert mul == 1.2

    def test_завтра_10(self):
        # 24 часа → ≥ 12 → ×1.0
        mul, label = calculate_urgency_multiplier("завтра")
        assert mul == 1.0
        assert "стандартн" in label.lower()

    def test_3_дня_10(self):
        mul, _ = calculate_urgency_multiplier("через 3 дня")
        assert mul == 1.0

    def test_2_часа_15(self):
        mul, _ = calculate_urgency_multiplier("через 2 часа")
        assert mul == 1.5

    def test_10_часов_12(self):
        mul, _ = calculate_urgency_multiplier("через 10 часов")
        assert mul == 1.2

    def test_none_defaults_to_10(self):
        mul, _ = calculate_urgency_multiplier(None)
        assert mul == 1.0

    def test_empty_defaults_to_10(self):
        mul, _ = calculate_urgency_multiplier("")
        assert mul == 1.0

    def test_garbage_defaults_to_10(self):
        mul, _ = calculate_urgency_multiplier("какая-то ерунда")
        assert mul == 1.0


# ─── Применение коэффициента в полной формуле ───────────────────────────────

class TestPricingWithUrgency:
    """Интеграционные тесты — итоговая цена с применением коэффициента."""

    def test_kursovaya_срочно_15(self):
        # 30 стр. × 100 = 3000 базовая, × 1.5 (срочно) = 4500
        price, _, info = calculate_starting_price("kursovaya", 30, "срочно")
        assert info["base_price"] == 3000
        assert info["urgency_multiplier"] == 1.5
        assert price == 4500

    def test_referat_завтра_no_change(self):
        price, _, info = calculate_starting_price("referat", 15, "завтра")
        assert info["urgency_multiplier"] == 1.0
        assert price == info["base_price"]

    def test_konspekt_сегодня_12(self):
        # База 200 × 1.2 = 240
        price, _, info = calculate_starting_price("konspekt", 10, "сегодня")
        assert info["urgency_multiplier"] == 1.2
        assert price == 240

    def test_prezentaciya_no_deadline(self):
        # Без дедлайна — без коэффициента
        price, _, info = calculate_starting_price("prezentaciya", 10, None)
        assert info["urgency_multiplier"] == 1.0
        assert price == 450


# ─── Регрессия: соответствие лендингу ────────────────────────────────────────

class TestLandingPrice:
    """Минимальные цены на лендинге должны совпадать с тарифом."""

    def test_kursovaya_landing_2000(self):
        price, _, _ = calculate_starting_price("kursovaya", 20)
        assert price == 2000

    def test_referat_landing_300(self):
        price, _, _ = calculate_starting_price("referat", 10)
        assert price == 300

    def test_statya_landing_1200(self):
        price, _, _ = calculate_starting_price("statya", 6)
        assert price == 1200

    def test_konspekt_landing_200(self):
        price, _, _ = calculate_starting_price("konspekt", 10)
        assert price == 200

    def test_prezentaciya_landing_450(self):
        price, _, _ = calculate_starting_price("prezentaciya", 10)
        assert price == 450

    def test_domashka_landing_300(self):
        price, _, _ = calculate_starting_price("domashka", 1)
        assert price == 300
