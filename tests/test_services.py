"""
Тесты сервисов: prompt_builder, template_selector, payment (parse_webhook).
"""
import os
import pytest
from unittest.mock import patch, MagicMock


# ─── Тесты prompt_builder ────────────────────────────────────────────────────

class TestPromptBuilder:
    def _make_order(self, work_type="kursovaya", topic="Экономика труда",
                    subject="Экономика", pages=25, comment=None):
        order = MagicMock()
        order.work_type = work_type
        order.topic = topic
        order.subject = subject
        order.pages = pages
        order.comment = comment
        return order

    def test_prompt_contains_topic(self):
        from services.prompt_builder import build_prompt
        order = self._make_order(topic="Управление рисками")
        prompt = build_prompt(order)
        assert "Управление рисками" in prompt

    def test_prompt_contains_subject(self):
        from services.prompt_builder import build_prompt
        order = self._make_order(subject="Менеджмент")
        prompt = build_prompt(order)
        assert "Менеджмент" in prompt

    def test_prompt_contains_page_count(self):
        from services.prompt_builder import build_prompt
        order = self._make_order(pages=30)
        prompt = build_prompt(order)
        assert "30" in prompt

    def test_prompt_contains_comment(self):
        from services.prompt_builder import build_prompt
        order = self._make_order(comment="Обязательно про инфляцию")
        prompt = build_prompt(order)
        assert "Обязательно про инфляцию" in prompt

    def test_prompt_no_comment_when_none(self):
        from services.prompt_builder import build_prompt
        order = self._make_order(comment=None)
        prompt = build_prompt(order)
        assert "Дополнительные требования" not in prompt

    def test_all_work_types_generate_prompt(self):
        from services.prompt_builder import build_prompt, WORK_TYPE_DISPLAY
        for wt in WORK_TYPE_DISPLAY:
            order = self._make_order(work_type=wt)
            prompt = build_prompt(order)
            assert len(prompt) > 50

    def test_sources_count_increases_with_pages(self):
        from services.prompt_builder import build_prompt, _calc_sources
        assert _calc_sources(5) == 5     # min 5
        assert _calc_sources(50) == 10   # 50 // 5
        assert _calc_sources(100) == 20

    def test_student_file_instruction(self):
        from services.prompt_builder import build_prompt
        order = self._make_order()
        prompt = build_prompt(order, has_student_file=True)
        assert "прикреплённом файле" in prompt

    def test_russian_language_instruction(self):
        from services.prompt_builder import build_prompt
        order = self._make_order()
        prompt = build_prompt(order)
        assert "русском языке" in prompt


# ─── Тесты template_selector ─────────────────────────────────────────────────

class TestTemplateSelector:
    def test_student_file_takes_priority(self, tmp_path):
        from services.template_selector import get_template_path
        student_file = tmp_path / "student.docx"
        student_file.write_text("fake docx")
        result = get_template_path("kursovaya", str(student_file))
        assert result == str(student_file)

    def test_returns_none_if_no_template(self):
        from services.template_selector import get_template_path
        with patch("services.template_selector.settings") as mock_s:
            mock_s.TEMPLATES_DIR = "/nonexistent/path"
            result = get_template_path("kursovaya", student_file_path=None)
        assert result is None

    def test_returns_none_for_missing_student_file(self):
        from services.template_selector import get_template_path
        result = get_template_path("kursovaya", "/nonexistent/file.docx")
        # Если файла студента нет — пробуем шаблон
        # Если и шаблона нет — None
        # Просто не должно бросать исключение
        assert result is None or isinstance(result, str)

    def test_all_work_types_handled(self):
        from services.template_selector import TEMPLATE_MAP
        for wt in ["kursovaya", "referat", "statya", "domashka",
                   "konspekt", "prezentaciya", "drugoe"]:
            assert wt in TEMPLATE_MAP


# ─── Тесты Robokassa Result URL ──────────────────────────────────────────────

class TestRobokassaResult:
    """Тесты разбора параметров Result URL от Робокассы и проверки подписи."""

    def _setup_test_passwords(self, monkeypatch):
        """Подкладываем тестовые пароли в settings."""
        from config import settings
        monkeypatch.setattr(settings, "ROBOKASSA_PASSWORD_2", "test_pwd_2")
        monkeypatch.setattr(settings, "ROBOKASSA_TEST_PASSWORD_2", "test_pwd_2")
        monkeypatch.setattr(settings, "ROBOKASSA_IS_TEST", True)
        monkeypatch.setattr(settings, "ROBOKASSA_HASH_ALGO", "sha256")

    def test_signature_check_valid(self, monkeypatch):
        """Корректная подпись проходит проверку."""
        self._setup_test_passwords(monkeypatch)
        from services.payment import _hash, verify_result_signature
        expected_signature = _hash("100.00:42:test_pwd_2")
        assert verify_result_signature("100.00", "42", expected_signature) is True

    def test_signature_check_invalid(self, monkeypatch):
        """Подделанная подпись отвергается."""
        self._setup_test_passwords(monkeypatch)
        from services.payment import verify_result_signature
        assert verify_result_signature("100.00", "42", "WRONG_SIGNATURE") is False

    def test_signature_case_insensitive(self, monkeypatch):
        """Подпись сравнивается без учёта регистра (Robokassa возвращает upper)."""
        self._setup_test_passwords(monkeypatch)
        from services.payment import _hash, verify_result_signature
        sig = _hash("100.00:42:test_pwd_2")
        assert verify_result_signature("100.00", "42", sig.lower()) is True
        assert verify_result_signature("100.00", "42", sig.upper()) is True

    def test_parse_valid_result(self, monkeypatch):
        """Корректный набор параметров парсится без ошибки."""
        self._setup_test_passwords(monkeypatch)
        from services.payment import _hash, parse_result_params
        sig = _hash("500.00:7:test_pwd_2")
        inv_id, amount, is_valid = parse_result_params({
            "OutSum": "500.00",
            "InvId": "7",
            "SignatureValue": sig,
        })
        assert inv_id == "7"
        assert amount == 500.00
        assert is_valid is True

    def test_parse_missing_params_raises(self):
        from services.payment import parse_result_params
        with pytest.raises(ValueError, match="отсутствуют"):
            parse_result_params({"OutSum": "100.00"})  # нет InvId и SignatureValue

    def test_parse_bad_amount_raises(self, monkeypatch):
        self._setup_test_passwords(monkeypatch)
        from services.payment import parse_result_params
        with pytest.raises(ValueError, match="OutSum"):
            parse_result_params({
                "OutSum": "abc",
                "InvId": "1",
                "SignatureValue": "x",
            })

    def test_build_result_response(self):
        """Ответ Робокассе должен быть `OK<InvId>`."""
        from services.payment import build_result_response
        assert build_result_response("42") == "OK42"
        assert build_result_response("100500") == "OK100500"


class TestRobokassaPaymentURL:
    """Тесты формирования URL оплаты."""

    async def test_create_payment_url_contains_login(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "ROBOKASSA_LOGIN", "pishu24")
        monkeypatch.setattr(settings, "ROBOKASSA_TEST_PASSWORD_1", "test_pwd_1")
        monkeypatch.setattr(settings, "ROBOKASSA_IS_TEST", True)
        monkeypatch.setattr(settings, "ROBOKASSA_HASH_ALGO", "sha256")
        monkeypatch.setattr(settings, "ROBOKASSA_FALLBACK_EMAIL", "test@example.com")

        from services.payment import create_payment_url
        result = await create_payment_url(
            order_id=42, amount=1000.0,
            description="Заказ #42",
            customer_email="user@example.com",
        )
        assert "pishu24" in result["confirmation_url"]
        assert "MerchantLogin=pishu24" in result["confirmation_url"]
        assert "InvId=42" in result["confirmation_url"]
        assert "OutSum=1000.00" in result["confirmation_url"]
        assert "IsTest=1" in result["confirmation_url"]  # тестовый режим
        assert "SignatureValue=" in result["confirmation_url"]
        assert result["payment_id"] == "42"

    async def test_create_payment_url_requires_login(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "ROBOKASSA_LOGIN", "")
        from services.payment import create_payment_url
        with pytest.raises(RuntimeError, match="не настроена"):
            await create_payment_url(order_id=1, amount=100.0, description="test")

    async def test_create_payment_url_requires_password(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "ROBOKASSA_LOGIN", "pishu24")
        monkeypatch.setattr(settings, "ROBOKASSA_TEST_PASSWORD_1", "")
        monkeypatch.setattr(settings, "ROBOKASSA_IS_TEST", True)
        from services.payment import create_payment_url
        with pytest.raises(RuntimeError, match="пароль"):
            await create_payment_url(order_id=1, amount=100.0, description="test")
