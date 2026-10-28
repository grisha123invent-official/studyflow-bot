"""
Конвертация .docx → .pdf и наложение водяного знака через PyMuPDF (fitz).
Для конвертации используется LibreOffice CLI.
"""
import os
import subprocess
import logging
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def _convert_docx_to_pdf_pure_python(docx_path: str, pdf_path: str) -> None:
    """
    Чистый Python: python-docx → fpdf2.
    Работает везде без сторонних бинарников. Простое форматирование (текст, абзацы).
    """
    from docx import Document
    from fpdf import FPDF

    doc = Document(docx_path)

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    # Подбираем системный шрифт с Кириллицей
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",       # macOS
        "/Library/Fonts/Arial Unicode.ttf",                    # macOS legacy
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",    # Linux
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",                       # Windows
    ]
    font_path = next((p for p in candidates if os.path.isfile(p)), None)

    if font_path:
        pdf.add_font("Custom", "", font_path)
        pdf.set_font("Custom", size=12)
    else:
        logger.warning("Не найден системный шрифт с Кириллицей — текст может не отобразиться корректно")
        pdf.set_font("Helvetica", size=12)

    for para in doc.paragraphs:
        text = para.text or ""
        if not text.strip():
            pdf.ln(6)
            continue
        try:
            pdf.multi_cell(0, 8, text)
        except Exception as e:
            logger.error(f"fpdf2 не смог отрендерить абзац: {e}")
            continue

    # Таблицы — простой текст
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text for cell in row.cells)
            try:
                pdf.multi_cell(0, 8, row_text)
            except Exception:
                pass

    pdf.output(pdf_path)


def _convert_office_to_pdf(input_path: str, output_dir: str) -> str:
    """
    Конвертирует офисный документ (.docx / .pptx) в .pdf.

    Презентации (.pptx / .ppt) конвертируются ТОЛЬКО через LibreOffice (модуль
    Impress) — чистого Python-fallback для них нет.

    Для .docx работает полная цепочка fallback:
      1. LibreOffice/soffice  — если установлен в PATH (Linux/Mac/Win)
      2. docx2pdf             — через MS Word (Mac/Windows с Word)
      3. python-docx + fpdf2  — чистый Python, работает везде (упрощённое форматирование)
    """
    ext = os.path.splitext(input_path)[1].lower()
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    pdf_path = os.path.join(output_dir, f"{base_name}.pdf")

    # PDF уже готов — конвертировать не нужно, возвращаем как есть.
    # (add_watermark не удалит исходник: там стоит защита pdf_path != input_path.)
    if ext == ".pdf":
        return input_path

    # 1) LibreOffice — работает и для .docx, и для .pptx
    import shutil
    import tempfile
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if soffice:
        # ВАЖНО: отдельный чистый профиль на каждую конвертацию через
        # -env:UserInstallation. Без него Impress на сервере (headless, под
        # пользователем bot) падает с DeploymentException / "javaldx", т.к.
        # дефолтный профиль в $HOME недоступен/залочен. Профиль удаляем после.
        profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
        try:
            result = subprocess.run(
                [
                    soffice, "--headless",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to", "pdf", "--outdir", output_dir, input_path,
                ],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode == 0 and os.path.isfile(pdf_path):
                logger.info("PDF создан через LibreOffice")
                return pdf_path
            logger.warning(f"LibreOffice вернул {result.returncode}: {result.stderr}")
        except Exception as e:
            logger.warning(f"LibreOffice сбой: {e}")
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)

    # Для презентаций fallback'ов нет — нужен LibreOffice (Impress)
    if ext in (".pptx", ".ppt"):
        raise RuntimeError(
            "Не удалось конвертировать презентацию в PDF. "
            "На сервере должен быть установлен LibreOffice с модулем Impress "
            "(apt install libreoffice-impress)."
        )

    # 2) docx2pdf (MS Word на Mac/Windows) — только для .docx
    try:
        from docx2pdf import convert
        convert(input_path, pdf_path)
        if os.path.isfile(pdf_path):
            logger.info("PDF создан через docx2pdf (Word)")
            return pdf_path
    except Exception as e:
        logger.warning(f"docx2pdf сбой: {e}")

    # 3) Чистый Python — последний шанс (только для .docx)
    logger.info("Использую pure-Python fallback (python-docx + fpdf2)")
    _convert_docx_to_pdf_pure_python(input_path, pdf_path)
    if not os.path.isfile(pdf_path):
        raise RuntimeError("Все методы конвертации не сработали")
    return pdf_path


def _convert_docx_to_pdf(docx_path: str, output_dir: str) -> str:
    """Обратная совместимость: конвертация .docx → .pdf. См. _convert_office_to_pdf."""
    return _convert_office_to_pdf(docx_path, output_dir)


def _find_cyrillic_font() -> str | None:
    """Ищет системный TTF-шрифт с поддержкой кириллицы."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\times.ttf",
    ]
    return next((p for p in candidates if os.path.isfile(p)), None)


def _apply_watermark_to_pdf(
    pdf_path: str,
    order_id: int,
    output_path: str,
    variant: int = 0,
    flatten: bool = False,
) -> None:
    """
    Накладывает диагональный водяной знак на каждую страницу PDF.
    Использует TextWriter + Matrix(45) для произвольного угла поворота.

    :param flatten: если True — каждая страница растеризуется в изображение,
        и водяной знак «впекается» в пиксели. В итоговом PDF нет текстового
        слоя, поэтому знак невозможно удалить редактированием. Используется
        для презентаций (нельзя удалить вотермарку).
    """
    doc = fitz.open(pdf_path)
    font_path = _find_cyrillic_font()

    from config import settings
    line1 = getattr(settings, "WATERMARK_TEXT", "ОПЛАТИТЕ ДЛЯ ПОЛУЧЕНИЯ") or "ОПЛАТИТЕ ДЛЯ ПОЛУЧЕНИЯ"
    line2 = f"Заказ #{order_id}"

    # Варианты расположения: разные позиции и углы при пересоздании
    variants = [
        {"angle": 45,  "dx1": -150, "dy1": -20, "dx2": -70, "dy2": 30},
        {"angle": -45, "dx1": -150, "dy1": 40,  "dx2": -70, "dy2": -20},
        {"angle": 30,  "dx1": -180, "dy1": 0,   "dx2": -60, "dy2": 40},
        {"angle": 60,  "dx1": -100, "dy1": -40, "dx2": -50, "dy2": 20},
    ]
    v = variants[variant % len(variants)]

    for page in doc:
        w = page.rect.width
        h = page.rect.height
        pivot = fitz.Point(w / 2, h / 2)
        rot_mat = fitz.Matrix(v["angle"])

        try:
            font = fitz.Font(fontfile=font_path) if font_path else fitz.Font("helv")
            tw = fitz.TextWriter(page.rect, color=(0.8, 0.0, 0.0))
            tw.append(fitz.Point(w / 2 + v["dx1"], h / 2 + v["dy1"]), line1, font=font, fontsize=32)
            tw.append(fitz.Point(w / 2 + v["dx2"], h / 2 + v["dy2"]), line2, font=font, fontsize=24)
            tw.write_text(page, morph=(pivot, rot_mat), opacity=0.35)
        except Exception as e:
            logger.warning(f"TextWriter сбой ({e}), использую insert_textbox")
            rect = fitz.Rect(50, h / 2 - 60, w - 50, h / 2 + 60)
            page.insert_textbox(
                rect,
                f"{line1}\n{line2}",
                fontsize=28,
                color=(0.8, 0.0, 0.0),
                align=fitz.TEXT_ALIGN_CENTER,
            )

    # Растеризация: «впекаем» водяной знак в пиксели, убирая текстовый слой,
    # чтобы знак нельзя было удалить редактированием PDF (для презентаций).
    if flatten:
        flat = fitz.open()
        mat = fitz.Matrix(2, 2)  # 2x — компромисс между качеством и размером
        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            # JPEG вместо PNG — иначе презентация на 10–15 слайдов
            # раздувается до десятков МБ и не влезает в лимит Telegram (50 МБ).
            jpg = pix.tobytes("jpeg", jpg_quality=75)
            new_page = flat.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=jpg)
        flat.save(output_path, deflate=True)
        flat.close()
        doc.close()
        return

    doc.save(output_path)
    doc.close()


def add_watermark(input_path: str, order_id: int, variant: int = 0) -> str:
    """
    Полный пайплайн: .docx/.pptx → .pdf → .pdf с водяным знаком.

    Превью ВСЕГДА растеризуется (flatten): водяной знак впекается в пиксели,
    в итоговом PDF нет текстового слоя — знак невозможно удалить
    редактированием файла. Работает одинаково и для документов, и для
    презентаций.

    :param input_path: путь к исходному .docx или .pptx
    :param order_id: номер заказа
    :param variant: номер варианта расположения знака (для пересоздания)
    :return: путь к PDF с водяным знаком
    """
    output_dir = os.path.dirname(input_path)

    logger.info(f"Конвертируем {input_path} в PDF...")
    pdf_path = _convert_office_to_pdf(input_path, output_dir)

    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    watermarked_path = os.path.join(output_dir, f"{base_name}_preview_v{variant}.pdf")

    logger.info(f"Накладываем водяной знак (вариант {variant}, flatten) → {watermarked_path}")
    _apply_watermark_to_pdf(
        pdf_path, order_id, watermarked_path, variant=variant, flatten=True
    )

    # Удаляем промежуточный PDF без водяного знака.
    # НО если исходник сам был PDF (pdf_path == input_path) — не трогаем его,
    # это чистый финальный файл, который уйдёт студенту после оплаты.
    if pdf_path != input_path:
        try:
            os.remove(pdf_path)
        except OSError:
            pass

    return watermarked_path


def _merge_pdfs(pdf_paths: list[str], output_path: str) -> str:
    """Склеивает несколько PDF в один (в переданном порядке)."""
    merged = fitz.open()
    try:
        for p in pdf_paths:
            with fitz.open(p) as src:
                merged.insert_pdf(src)
        merged.save(output_path, deflate=True)
    finally:
        merged.close()
    return output_path


def add_watermark_merged(input_paths: list[str], order_id: int, variant: int = 0) -> str:
    """
    Несколько исходников (.docx/.pptx, можно вперемешку) → по PDF с водяным
    знаком на каждый → склейка в ОДИН PDF-превью.

    Это то, что уходит студенту/редактору: один файл, а не пачка. Водяной знак
    впекается в пиксели каждой страницы (см. add_watermark), поэтому в склеенном
    PDF его тоже нельзя удалить.

    :param input_paths: список путей к исходным файлам (по одной работе на файл)
    :return: путь к объединённому PDF с водяным знаком
    """
    if not input_paths:
        raise ValueError("add_watermark_merged: пустой список файлов")

    output_dir = os.path.dirname(input_paths[0])

    parts: list[str] = [add_watermark(src, order_id, variant=variant) for src in input_paths]

    # Один файл — склеивать нечего
    if len(parts) == 1:
        return parts[0]

    merged_path = os.path.join(output_dir, f"order_{order_id}_preview_v{variant}.pdf")
    _merge_pdfs(parts, merged_path)

    # Удаляем промежуточные одиночные превью — остаётся только склейка
    for p in parts:
        if os.path.abspath(p) != os.path.abspath(merged_path):
            try:
                os.remove(p)
            except OSError:
                pass

    return merged_path
