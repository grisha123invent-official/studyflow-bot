"""
Расчёт предварительной (стартовой) стоимости заказа.

Логика:
    price = базовая_ставка × объём × коэффициент_срочности

Базовая ставка синхронизирована с лендингом
(раздел «Из чего складывается цена»):
- Курсовая работа: 100 ₽/стр. (мин. 2000 ₽)
- Реферат: 30 ₽/стр. (мин. 300 ₽)
- Научная статья: 200 ₽/стр. (мин. 1200 ₽)
- Конспект: 20 ₽/стр. (мин. 200 ₽, от 10 стр.)
- Презентация: 45 ₽/слайд (мин. 450 ₽)
- Домашнее задание: 300 ₽ за работу

Коэффициент срочности (× 1.0 – 1.5):
- < 6 часов  → × 1.5 (+50%)
- < 12 часов → × 1.2 (+20%)
- ≥ 12 часов или непонятный дедлайн → × 1.0

Остальные коэффициенты (сложность темы, источники, уникальность)
применяет администратор вручную при согласовании финальной цены.
"""
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger("pricing")


# ─── Тарифы по типам работ ───────────────────────────────────────────────────
# Структура: {work_type: {"base": базовая_минимальная,
#                          "per_unit": цена_за_единицу,
#                          "min_units": минимально_оплачиваемый_объём,
#                          "unit": подпись}}

PRICING_RULES = {
    # Курсовая: 100 ₽/стр., минимум 2000 ₽ (при объёме до 20 стр.)
    "kursovaya":    {"base": 2000, "per_unit": 100, "min_units": 20, "unit": "стр."},

    # Реферат: 30 ₽/стр., минимум 300 ₽ (при объёме до 10 стр.)
    "referat":      {"base": 300, "per_unit": 30, "min_units": 10, "unit": "стр."},

    # Научная статья: 200 ₽/стр., минимум 1200 ₽ (при объёме до 6 стр.)
    "statya":       {"base": 1200, "per_unit": 200, "min_units": 6, "unit": "стр."},

    # Конспект: 20 ₽/стр., минимум 200 ₽ (при объёме до 10 стр.)
    "konspekt":     {"base": 200, "per_unit": 20, "min_units": 10, "unit": "стр."},

    # Презентация: 45 ₽/слайд, минимум 450 ₽ (при объёме до 10 слайдов)
    "prezentaciya": {"base": 450, "per_unit": 45, "min_units": 10, "unit": "слайд"},

    # Домашнее задание: фиксированная 300 ₽ за «работу»
    "domashka":     {"base": 300, "per_unit": 0, "min_units": 1, "unit": "работа"},

    # Другое: 100 ₽ за страницу, минимум 500 ₽
    "drugoe":       {"base": 500, "per_unit": 100, "min_units": 5, "unit": "стр."},
}


# ─── Парсер срочности ─────────────────────────────────────────────────────────

# Месяцы для парсинга «15 января», «к 5 марта» и т.п.
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _parse_deadline_to_hours(deadline: str) -> Optional[float]:
    """
    Парсит произвольную строку дедлайна и возвращает кол-во часов от текущего момента.
    Возвращает None если не получилось распознать.

    Поддерживает:
    - «срочно», «асап», «как можно скорее»     → 4 часа
    - «сегодня», «к вечеру», «за ночь»          → 8 часов
    - «до утра», «к утру»                       → 12 часов
    - «завтра», «к завтра», «до завтра»         → 24 часа
    - «послезавтра»                              → 48 часов
    - «через 3 дня», «через 2 часа», «через 2 нед.» → точно
    - «3 дня», «5 дней», «1 неделя», «2 часа»   → точно
    - «15.01», «15.01.2026», «01/02/2026»        → дата
    - «15 января», «5 марта»                    → дата
    """
    if not deadline or not isinstance(deadline, str):
        return None
    text = deadline.lower().strip()
    now = datetime.now()

    # 1. Ключевые слова «срочно»
    if re.search(r"(срочн|асап|немедл|как можно скор|asap)", text):
        return 4.0

    # 2. «сегодня», «к вечеру», «за ночь»
    if re.search(r"(сегодня|к вечеру|за ночь|сейчас)", text):
        return 8.0

    # 3. «до утра», «к утру»
    if re.search(r"(до утра|к утру|с утра)", text):
        return 12.0

    # 4. «послезавтра»
    if re.search(r"послезавтр", text):
        return 48.0

    # 5. «завтра» (но не «послезавтра» — поймали выше)
    if re.search(r"завтра", text):
        return 24.0

    # 6. «через X часов/дней/недель» или просто «X часов/дней/недель»
    # часы
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:час|ч\.?|h)", text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    # дни
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:дн|сут)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 24
        except ValueError:
            pass
    # недели
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:нед)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 24 * 7
        except ValueError:
            pass
    # месяцы (через)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мес)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ".")) * 24 * 30
        except ValueError:
            pass

    # 7. Даты вида «15.01», «15.01.2026», «01/02/2026»
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", text)
    if m:
        try:
            day = int(m.group(1))
            month = int(m.group(2))
            year_str = m.group(3)
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                year = now.year
                # Если эта дата уже прошла в этом году — берём следующий
                try:
                    candidate = datetime(year, month, day)
                    if candidate < now:
                        year += 1
                except ValueError:
                    pass
            target = datetime(year, month, day, 23, 59, 0)
            delta = (target - now).total_seconds() / 3600
            if delta > 0:
                return delta
        except (ValueError, TypeError):
            pass

    # 8. Дата словами «15 января», «к 5 марта»
    m = re.search(r"(\d{1,2})\s+([а-яё]+)", text)
    if m:
        try:
            day = int(m.group(1))
            month_word = m.group(2)
            month_num = None
            for prefix, num in _MONTHS.items():
                if month_word.startswith(prefix):
                    month_num = num
                    break
            if month_num:
                year = now.year
                try:
                    candidate = datetime(year, month_num, day)
                    if candidate < now:
                        year += 1
                except ValueError:
                    pass
                target = datetime(year, month_num, day, 23, 59, 0)
                delta = (target - now).total_seconds() / 3600
                if delta > 0:
                    return delta
        except (ValueError, TypeError):
            pass

    return None


def calculate_urgency_multiplier(deadline: Optional[str]) -> Tuple[float, str]:
    """
    Возвращает (множитель, человекочитаемое описание).

    < 6 ч  → × 1.5 (+50%, «срочно»)
    < 12 ч → × 1.2 (+20%, «быстро»)
    иначе  → × 1.0 (без наценки, «стандартный срок»)
    """
    hours = _parse_deadline_to_hours(deadline or "")
    if hours is None:
        return 1.0, "стандартный срок"
    if hours < 6:
        return 1.5, f"срочный заказ (≈{hours:.0f}ч)"
    if hours < 12:
        return 1.2, f"быстрый заказ (≈{hours:.0f}ч)"
    return 1.0, "стандартный срок"


# ─── Главная функция расчёта ──────────────────────────────────────────────────

def calculate_starting_price(
    work_type: Optional[str],
    pages: Optional[int],
    deadline: Optional[str] = None,
) -> Tuple[int, str, dict]:
    """
    Рассчитывает стартовую стоимость заказа.

    :param work_type: ключ типа работы (kursovaya, referat, statya, konspekt,
                      domashka, prezentaciya, drugoe). Может быть None.
    :param pages: количество страниц / слайдов. Может быть None или 0.
    :param deadline: текст дедлайна для расчёта коэффициента срочности.
    :return: (final_price, unit_label, info_dict), где info_dict содержит:
        - base_price: базовая цена до коэффициентов
        - urgency_multiplier: коэффициент срочности (1.0 / 1.2 / 1.5)
        - urgency_label: человеческое описание срочности
    """
    pages = pages or 1

    # Базовая цена по типу работы
    rule = PRICING_RULES.get(work_type or "drugoe", PRICING_RULES["drugoe"])
    base = rule["base"]
    per_unit = rule["per_unit"]
    min_units = rule["min_units"]
    unit = rule["unit"]

    # base — минимальная стоимость, действует пока объём не превысил min_units
    if pages <= min_units:
        base_price = base
    else:
        extra_units = pages - min_units
        base_price = base + extra_units * per_unit

    # Коэффициент срочности
    urgency_mul, urgency_label = calculate_urgency_multiplier(deadline)

    # Финальная стартовая цена
    final_price = int(round(base_price * urgency_mul))

    # Подпись объёма
    if unit == "работа":
        unit_label = "1 работа"
    else:
        unit_label = f"{pages} {unit}"

    info = {
        "base_price": int(base_price),
        "urgency_multiplier": urgency_mul,
        "urgency_label": urgency_label,
    }

    return final_price, unit_label, info
