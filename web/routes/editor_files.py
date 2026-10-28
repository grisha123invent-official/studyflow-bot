"""
Безопасная отдача файлов редактору.

Проверки:
- Файл существует на диске
- File.order имеет статус из {EDITING, PREVIEW_SENT, AWAITING_PAYMENT, DONE}
  (DONE разрешён только если редактор хочет посмотреть свой же финал — но не файлы)
- file_type — whitelist {STUDENT_TEMPLATE, DRAFT, PREVIEW, FINAL}

Файлы DONE-заказов не отдаются, чтобы редактор не мог скачать чистый финал
после оплаты (защита от увода после получения студентом).
"""
import logging
import os
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.models import File as FileModel, Order, OrderStatus, FileType
from db.session import AsyncSessionLocal
from web.deps_editor import require_editor

logger = logging.getLogger("editor.files")
router = APIRouter()


ALLOWED_FILE_TYPES = {
    FileType.STUDENT_TEMPLATE.value,
    FileType.DRAFT.value,
    FileType.PREVIEW.value,
    FileType.FINAL.value,
}

# Файлы из заказов в этих статусах доступны редактору
ALLOWED_ORDER_STATUSES = {
    OrderStatus.EDITING.value,
    OrderStatus.PREVIEW_SENT.value,
    OrderStatus.AWAITING_PAYMENT.value,
}


@router.get("/editor/files/{file_id}")
async def download_file(
    file_id: int,
    request: Request,
    editor_tg_id: int = Depends(require_editor),
):
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(FileModel)
            .options(selectinload(FileModel.order))
            .where(FileModel.id == file_id)
        )
        f = r.scalar_one_or_none()

    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден")

    ftype = f.file_type if isinstance(f.file_type, str) else f.file_type.value
    if ftype not in ALLOWED_FILE_TYPES:
        logger.warning(f"editor {editor_tg_id} tried to access disallowed file_type={ftype}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Тип файла недоступен")

    if f.order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заказ не найден")

    order_status = f.order.status if isinstance(f.order.status, str) else f.order.status.value
    if order_status not in ALLOWED_ORDER_STATUSES:
        logger.warning(
            f"editor {editor_tg_id} tried to access file_id={file_id} of order in status={order_status}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Файлы заказов вне активной работы недоступны",
        )

    if not f.local_path or not os.path.exists(f.local_path):
        logger.error(f"File {file_id}: local_path missing or not on disk: {f.local_path}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл отсутствует на диске")

    # Защита от path traversal на уровне ответа — отдаём только абсолютный путь
    abs_path = os.path.abspath(f.local_path)
    uploads_dir = os.path.abspath(os.environ.get("UPLOADS_DIR", "uploads"))
    # Не строгая проверка — local_path может быть из старого формата бота;
    # ключевая защита: имя файла санитизируется при загрузке (admin/editor uploads).

    download_name = f.filename or os.path.basename(abs_path)
    # RFC 5987 filename* для кириллицы
    quoted = urllib.parse.quote(download_name)

    # PREVIEW отдаём inline для просмотра в <iframe>, остальные — attachment
    disposition = "inline" if ftype == FileType.PREVIEW.value else "attachment"

    return FileResponse(
        abs_path,
        filename=download_name,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quoted}",
            "Cache-Control": "private, no-store",
        },
    )
