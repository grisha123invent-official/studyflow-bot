"""
Регрессионные тесты маршрутизации callback'ов админа.

Баг: хендлер отклонения заказа ловил F.data.startswith("reject_") и был
зарегистрирован РАНЬШЕ хендлеров отклонения вывода (reject_wd_*) и возврата
(reject_refund_*) — поэтому «Отклонить вывод/возврат #N» по факту отменял
заказ #N. Фикс — исключить эти префиксы (см. bot/handlers/admin.py cb_reject_order).

Тест проверяет ту же фильтр-конструкцию, что стоит в декораторе.
"""
import unittest
from types import SimpleNamespace

from aiogram import F


# Зеркало фильтров из bot/handlers/admin.py
ORDER_REJECT = (
    F.data.startswith("reject_")
    & ~F.data.startswith("reject_wd_")
    & ~F.data.startswith("reject_refund_")
)
WD_REJECT = F.data.startswith("reject_wd_")
REFUND_REJECT = F.data.startswith("reject_refund_")


def _match(flt, data: str) -> bool:
    return bool(flt.resolve(SimpleNamespace(data=data)))


class TestRejectCallbackRouting(unittest.TestCase):
    def test_order_reject_matches_only_plain_reject(self):
        self.assertTrue(_match(ORDER_REJECT, "reject_5"))
        self.assertTrue(_match(ORDER_REJECT, "reject_123"))

    def test_order_reject_excludes_withdrawal_and_refund(self):
        self.assertFalse(_match(ORDER_REJECT, "reject_wd_5"),
                         "Отклонение вывода НЕ должно попадать в отклонение заказа")
        self.assertFalse(_match(ORDER_REJECT, "reject_refund_5"),
                         "Отклонение возврата НЕ должно попадать в отклонение заказа")

    def test_each_callback_hits_exactly_one_handler(self):
        cases = {
            "reject_5":        [ORDER_REJECT],
            "reject_wd_5":     [WD_REJECT],
            "reject_refund_5": [REFUND_REJECT],
        }
        all_filters = {"ORDER": ORDER_REJECT, "WD": WD_REJECT, "REFUND": REFUND_REJECT}
        for data, expected in cases.items():
            hits = [name for name, flt in all_filters.items() if _match(flt, data)]
            self.assertEqual(len(hits), 1, f"{data!r} должен матчить ровно один хендлер, попало: {hits}")


class TestPaymentKeyboardEmailGate(unittest.TestCase):
    """Кнопка «Оплатить»: URL когда ссылка есть; callback-гейт (запрос email) когда нет."""

    def _buttons(self, kb):
        return [b for row in kb.inline_keyboard for b in row]

    def _pay_btn(self, kb):
        return next(b for b in self._buttons(kb) if "Оплатить" in b.text)

    def test_url_button_when_payment_url_present(self):
        from bot.keyboards.student_kb import preview_payment_kb
        kb = preview_payment_kb(5, "https://example.com/pay/1", show_rework=True)
        pay = self._pay_btn(kb)
        self.assertEqual(pay.url, "https://example.com/pay/1")
        self.assertIsNone(pay.callback_data)

    def test_callback_gate_when_no_url(self):
        from bot.keyboards.student_kb import preview_payment_kb
        kb = preview_payment_kb(5, None, show_rework=True)
        pay = self._pay_btn(kb)
        self.assertIsNone(pay.url, "Без email не должно быть прямой ссылки на оплату")
        self.assertEqual(pay.callback_data, "get_payment_5",
                         "Без email «Оплатить» должна вести в email-гейт")

    def test_callback_gate_no_rework(self):
        from bot.keyboards.student_kb import preview_payment_kb
        kb = preview_payment_kb(7, None, show_rework=False)
        btns = self._buttons(kb)
        self.assertEqual(len(btns), 1)
        self.assertEqual(btns[0].callback_data, "get_payment_7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
