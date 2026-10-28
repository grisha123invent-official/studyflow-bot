"""
FSM-состояния для заполнения заявки студентом.
"""
from aiogram.fsm.state import State, StatesGroup


class OrderStates(StatesGroup):
    """Шаги создания заявки через FSM"""
    work_type   = State()   # Выбор типа работы (кнопкой)
    topic       = State()   # Ввод темы
    subject     = State()   # Ввод предмета
    pages       = State()   # Количество страниц (число)
    deadline    = State()   # Дедлайн (текст)
    comment     = State()   # Комментарий / доп. требования (опционально)
    file_upload = State()   # Загрузка шаблона / методички (опционально)
    confirm     = State()   # Подтверждение заявки


class AdminPriceStates(StatesGroup):
    """Состояние для изменения цены заказа администратором"""
    waiting_price = State()  # Ожидание ввода новой цены


class AdminSendToEditorStates(StatesGroup):
    """Состояние отправки файлов редактору администратором"""
    collecting = State()  # Сбор файлов и комментария перед отправкой


class KonspektStates(StatesGroup):
    """Особый флоу для заказа конспекта"""
    title        = State()   # Название (тема конспекта)
    discipline   = State()   # Дисциплина
    deadline     = State()   # Дедлайн
    source_type  = State()   # Тип источника (лекция, вебинар, семинар...)
    source_link  = State()   # Ссылка на видео/аудио
    materials    = State()   # Доп. материалы (методичка, учебник) — файл или ссылка
    preferences  = State()   # Свои предпочтения
    confirm      = State()   # Подтверждение


class EditorStates(StatesGroup):
    """Состояние редактора при сборе финальных файлов"""
    collecting_final = State()  # Редактор прикрепляет финальные .docx


class EmailStates(StatesGroup):
    """Состояние ввода email для чека ЮКассы"""
    waiting_email = State()


class WithdrawalStates(StatesGroup):
    """Состояния для вывода средств студентом"""
    entering_amount = State()  # Ввод суммы
    waiting_card    = State()  # Ввод реквизитов карты
    confirm         = State()  # Подтверждение перед созданием заявки


class RefundStates(StatesGroup):
    """Состояния для подачи заявки на возврат студентом"""
    choosing_order  = State()  # Выбор заказа из списка оплаченных
    waiting_reason  = State()  # Ввод причины возврата


class AdminRefundStates(StatesGroup):
    """Состояния для отклонения возврата администратором"""
    waiting_rejection_reason = State()  # Ввод причины отклонения возврата


class SupportStates(StatesGroup):
    """Состояния для отправки сообщения в поддержку"""
    waiting_message = State()


class RevisionStates(StatesGroup):
    """Состояния для запроса правки клиентом"""
    waiting_description = State()


class ConsentStates(StatesGroup):
    """Состояния для процесса принятия оферты"""
    waiting_accept = State()


class PreviewReworkStates(StatesGroup):
    """Состояния для запроса доработки превью студентом (до оплаты)."""
    waiting_comment = State()


class PriceNegotiationStates(StatesGroup):
    """Состояния для торга по цене заявки (студент предлагает свою цену)."""
    waiting_student_offer = State()


class AdminNegotiationStates(StatesGroup):
    """Админ предлагает свою контрцену в ответ на торг студента."""
    waiting_counter = State()
