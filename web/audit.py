"""
Логирование действий админа в БД.

Каждое значимое действие в админ-панели должно вызывать `log_admin_action(...)`.
Это позволяет провести post-mortem при инциденте: кто, когда, что изменил.
"""
import json
import logging
from typing import Optional

from db.models import AdminAuditLog
from db.session import AsyncSessionLocal

logger = logging.getLogger("admin.audit")


async def log_admin_action(
    admin_tg_id: Optional[int],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """
    Записывает действие в БД. Не бросает исключений (audit не должен ломать flow).

    Действия: login_success, login_failed, login_unauthorized_tg_id, otp_sent,
              accept_order, reject_order, change_price, send_to_editor,
              approve_refund, reject_refund, approve_withdrawal, reject_withdrawal,
              ban_user, view_audit_log, ...
    """
    try:
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        async with AsyncSessionLocal() as session:
            entry = AdminAuditLog(
                admin_tg_id=admin_tg_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                ip_address=ip_address,
                user_agent=(user_agent or "")[:512],
                details=details_json,
            )
            session.add(entry)
            await session.commit()
        logger.info(
            f"AUDIT: admin={admin_tg_id} action={action} "
            f"target={target_type}:{target_id} ip={ip_address}"
        )
    except Exception as e:
        # Audit не должен ломать основной flow, только логируем ошибку
        logger.error(f"Failed to write audit log: {e}", exc_info=True)
