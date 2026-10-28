"""
Выбор шаблона .docx для заказа.
Логика: если студент прислал свой файл — используем его.
Иначе выбираем из папки /templates/ по типу работы.
"""
import os
from config import settings


# Маппинг типа работы → файл шаблона в папке /templates/
TEMPLATE_MAP = {
    "kursovaya": "tmpl_kursovaya.docx",
    "referat":   "tmpl_referat.docx",
    "statya":    "tmpl_statya.docx",
    "domashka":  "tmpl_base.docx",
    "konspekt":     "tmpl_base.docx",
    "prezentaciya": "tmpl_base.docx",
    "drugoe":       "tmpl_base.docx",
}


def get_template_path(
    work_type: str,
    student_file_path: str | None = None,
) -> str | None:
    """
    Возвращает путь к .docx-шаблону.

    :param work_type: тип работы из заказа (kursovaya, referat, ...)
    :param student_file_path: локальный путь к файлу студента (если загружен)
    :return: путь к файлу или None если шаблон не найден
    """
    # Приоритет — файл студента
    if student_file_path and os.path.isfile(student_file_path):
        return student_file_path

    # Выбираем наш шаблон
    template_filename = TEMPLATE_MAP.get(work_type, "tmpl_base.docx")
    template_path = os.path.join(settings.TEMPLATES_DIR, template_filename)

    if os.path.isfile(template_path):
        return template_path

    return None
