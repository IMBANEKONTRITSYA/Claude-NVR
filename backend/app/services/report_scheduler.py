"""Автоматическая отправка отчётов по расписанию (SPEC §8).

Планировщик без внешнего брокера: отдельный сервис (celery/APScheduler +
свой процесс) ради трёх расписаний на объекте — лишний узел, который надо
ставить, мониторить и перезапускать, а §26 требует пакет из четырёх
systemd-юнитов, а не из пяти.

Два свойства, которые здесь важнее простоты, и оба проверяются тестами:

1. **Ни одного дубля.** Слот «сегодня в 08:00» должен отработать ровно
   один раз, даже если процесс перезапустили в 08:00:30, а рядом крутится
   вторая реплика бэкенда (§17 допускает горизонтальное масштабирование).
2. **Ни одного молча пропущенного слота.** Если бэкенд лежал с 07:50 до
   08:10, отчёт за 08:00 должен уйти при старте, а не потеряться до
   завтра: «отчёт не пришёл» замечают через недели, а не сразу.

Оба свойства даёт одна и та же проверка: слот считается закрытым, если
`last_sent_at` не раньше его начала. Она же делает планировщик
идемпотентным — тик можно звать сколько угодно раз.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import anyio
from sqlalchemy import select

from ..db import SessionLocal
from ..models import ReportSchedule, Setting
from .encryption import decrypt_setting, SECRET_SETTING_KEYS
from .mailer import MailerError, send_email
from .reports import build_report, kind_title

logger = logging.getLogger("facewatch.reports")

# Как часто просыпаться. Минута — предел разрешения расписания (hour +
# minute), брать чаще незачем.
TICK_SEC = 60

PERIODS = ("daily", "weekly", "monthly")


def previous_slot(sched: ReportSchedule, now: datetime) -> datetime | None:
    """Начало последнего наступившего слота расписания, либо None.

    Считается «назад от now», а не «вперёд от last_sent_at»: при простое
    длиной в неделю проход вперёд выдал бы семь просроченных слотов и
    семь одинаковых писем подряд. Нужен ровно один — последний.
    """
    base = now.replace(second=0, microsecond=0)
    if sched.period == "daily":
        slot = base.replace(hour=sched.hour, minute=sched.minute)
        if slot > now:
            slot -= timedelta(days=1)
        return slot
    if sched.period == "weekly":
        slot = base.replace(hour=sched.hour, minute=sched.minute)
        delta = (slot.weekday() - sched.day_of_week) % 7
        slot -= timedelta(days=delta)
        if slot > now:
            slot -= timedelta(days=7)
        return slot
    if sched.period == "monthly":
        day = min(max(sched.day_of_month, 1), 28)
        slot = base.replace(day=day, hour=sched.hour, minute=sched.minute)
        if slot > now:
            # Предыдущий месяц: через первое число, чтобы не считать длины
            # месяцев руками.
            first = slot.replace(day=1) - timedelta(days=1)
            slot = first.replace(day=day, hour=sched.hour, minute=sched.minute)
        return slot
    return None


def is_due(sched: ReportSchedule, now: datetime) -> bool:
    """Пора ли отправлять: слот наступил и ещё не закрыт."""
    if not sched.enabled or sched.period not in PERIODS:
        return False
    slot = previous_slot(sched, now)
    if slot is None:
        return False
    return sched.last_sent_at is None or sched.last_sent_at < slot


async def _smtp_config(db) -> dict[str, str]:
    rows = (await db.execute(select(Setting))).scalars().all()
    return {s.key: (decrypt_setting(s.value) if s.key in SECRET_SETTING_KEYS else s.value)
            for s in rows}


async def send_scheduled_report(db, sched: ReportSchedule, cfg: dict[str, str]) -> bool:
    """Строит отчёт и отправляет его. True — письмо ушло.

    Получатели берутся из самого расписания, а `alert_email_to` служит
    запасным: отчёт «по объекту» обычно идёт тем же людям, что и алерты,
    и заставлять вводить адреса второй раз незачем.
    """
    filename, payload, _ = await build_report(db, sched.kind, sched.fmt, sched.days)
    recipients = sched.recipients or cfg.get("alert_email_to", "")
    subtype = "octet-stream" if sched.fmt == "xlsx" else "csv"
    body = (f"Отчёт «{sched.name}» ({kind_title(sched.kind)}) за последние "
            f"{sched.days} сут.\nСформирован автоматически системой FaceWatch.")
    return await anyio.to_thread.run_sync(
        lambda: send_email(
            cfg.get("smtp_host", ""), int(cfg.get("smtp_port") or 587),
            cfg.get("smtp_user", ""), cfg.get("smtp_password", ""),
            cfg.get("smtp_tls", "starttls"), cfg.get("smtp_from", ""),
            recipients, f"FaceWatch: отчёт «{sched.name}»", body,
            [(filename, payload, subtype)],
        )
    )


async def run_due(now: datetime | None = None, session_factory=None) -> int:
    """Один проход планировщика. Возвращает число отправленных отчётов.

    `session_factory` — точка подмены сессии. Пул asyncpg привязан к тому
    event loop'у, в котором создан (см. tests/conftest.py), поэтому вызов
    прохода из другого loop'а обязан работать со своим пулом; заодно это
    позволяет прогнать проход разовой командой, не поднимая приложение.

    Ошибка одного расписания не отменяет остальные и не роняет цикл:
    неверный адрес в одном отчёте не должен лишать объект всех прочих.
    Причина отказа сохраняется в `last_error` и видна в интерфейсе — без
    этого «отчёт не приходит» диагностировался бы только по логам сервера.
    """
    now = now or datetime.now()
    sent = 0
    async with (session_factory or SessionLocal)() as db:
        # FOR UPDATE держит строки до конца прохода: §17 допускает вторую
        # реплику бэкенда, и без блокировки обе прочитали бы один открытый
        # слот до того, как первая его закрыла, — отчёт ушёл бы дважды.
        # Проход идёт по единицам расписаний, поэтому удержание блокировки
        # на время отправки здесь дешевле, чем отдельный брокер или лок в
        # Redis (лишний узел ради того же результата).
        rows = (await db.execute(
            select(ReportSchedule).where(ReportSchedule.enabled.is_(True)).with_for_update()
        )).scalars().all()
        due = [s for s in rows if is_due(s, now)]
        if not due:
            return 0
        cfg = await _smtp_config(db)
        for sched in due:
            try:
                ok = await send_scheduled_report(db, sched, cfg)
            except MailerError as e:
                sched.last_error = str(e)[:500]
                logger.warning("отчёт по расписанию не отправлен",
                               extra={"schedule_id": sched.id, "error": str(e)})
                # last_sent_at НЕ двигаем: слот остаётся открытым, и
                # следующий тик попробует снова. Иначе единственная сетевая
                # ошибка отменяла бы отчёт до следующего слота.
                continue
            except Exception as e:
                sched.last_error = f"{type(e).__name__}: {e}"[:500]
                logger.error("ошибка построения отчёта по расписанию", exc_info=True,
                             extra={"schedule_id": sched.id})
                continue
            if ok:
                # Момент СЛОТА, а не now: иначе тик, случившийся на минуту
                # позже, сдвигал бы точку отсчёта и слот мог закрыться дважды.
                sched.last_sent_at = previous_slot(sched, now)
                sched.last_error = None
                sent += 1
                logger.info("отчёт отправлен по расписанию",
                            extra={"schedule_id": sched.id, "name": sched.name})
            else:
                sched.last_error = "Почта не настроена (SMTP-сервер или получатели)"
        await db.commit()
    return sent


async def scheduler_loop(stop: asyncio.Event) -> None:
    """Фоновый цикл. Живёт столько же, сколько приложение."""
    while not stop.is_set():
        try:
            await run_due()
        except Exception:
            # Цикл не имеет права умереть: без него отчёты просто перестают
            # приходить, и заметно это станет через сутки в лучшем случае.
            logger.error("сбой прохода планировщика отчётов", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SEC)
        except asyncio.TimeoutError:
            pass
