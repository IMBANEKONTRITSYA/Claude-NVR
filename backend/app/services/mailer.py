"""Отправка почты по SMTP (SPEC §11 «Настройки уведомлений: Telegram, email,
звук»).

SMTP требуют сразу три раздела ТЗ, и все три до этого модуля были не закрыты:
§6 «Алерты при детекции (Telegram, email, звук)», §8 «Автоматическая отправка
[отчётов] по расписанию (email)» и §11 «Настройки уведомлений». Поэтому здесь
только транспорт — что именно и кому слать, решают вызывающие.

Только stdlib: модуль зеркалится в воркере (`worker/mailer.py`, у него нет
зависимости от пакета `app`), а лишняя зависимость в двух requirements.txt
ради `smtplib` не нужна. Расхождение двух копий ловит
`backend/tests/test_mailer_parity.py`.

=== ОБЩАЯ ЧАСТЬ (сверяется с worker/mailer.py по AST) ===
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# Режимы шифрования канала. `starttls` — обычный порт 587 с апгрейдом
# соединения, `ssl` — implicit TLS на 465, `none` — открытый 25-й порт во
# внутренней сети объекта (типично для встроенного релея на самом сервере,
# где почта не выходит наружу).
TLS_MODES = ("none", "starttls", "ssl")

# Таймаут на всю SMTP-сессию. Отправка идёт из фонового потока воркера и из
# threadpool'а бэкенда, но без таймаута зависший релей удерживал бы поток
# бесконечно: у smtplib по умолчанию таймаут глобальный сокетный (обычно
# None), то есть «ждать вечно».
SMTP_TIMEOUT_SEC = 15


class MailerError(Exception):
    """Отправка не удалась. Текст безопасен для показа администратору:
    формируется из класса ошибки smtplib, а не из конфигурации."""


def split_recipients(raw: str) -> list[str]:
    """Список получателей из строки настройки.

    Разделители — запятая, точка с запятой и перевод строки: администратор
    вводит адреса руками в одно поле, и требовать ровно один разделитель
    значило бы молча терять получателей при вставке из другого списка.
    """
    if not raw:
        return []
    out = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        addr = chunk.strip()
        if addr and addr not in out:
            out.append(addr)
    return out


def build_message(sender: str, recipients: list[str], subject: str, body: str,
                  attachments: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:
    """Письмо с UTF-8 текстом и (опционально) вложениями.

    `attachments` — список `(имя файла, содержимое, mime-подтип)`; нужен
    §8 (отчёт Excel/CSV по расписанию), алертам §6 достаточно текста.
    Date и Message-ID проставляются явно: без них многие релеи помечают
    письмо как спам, а часть — отвергает.
    """
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="facewatch.local")
    msg.set_content(body)
    for filename, payload, subtype in attachments or []:
        msg.add_attachment(payload, maintype="application", subtype=subtype,
                           filename=filename)
    return msg


def send_message(msg: EmailMessage, host: str, port: int, user: str,
                 password: str, tls: str) -> None:
    """Синхронная отправка. Бросает MailerError с коротким описанием причины.

    Аутентификация выполняется только если задан логин: на внутреннем релее
    без авторизации `login("", "")` получил бы отказ 530, то есть настройка
    «хост есть, логина нет» была бы нерабочей.
    """
    context = ssl.create_default_context()
    try:
        if tls == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SEC,
                                      context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SEC)
        try:
            if tls == "starttls":
                server.starttls(context=context)
            if user:
                server.login(user, password)
            server.send_message(msg)
        finally:
            # quit() сам шлёт QUIT и закрывает сокет; на уже оборванном
            # соединении он бросает — тогда закрываем жёстко, иначе
            # исключение из закрытия подменило бы исходную причину отказа.
            try:
                server.quit()
            except Exception:
                server.close()
    except MailerError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise MailerError("SMTP: неверный логин или пароль")
    except smtplib.SMTPRecipientsRefused:
        raise MailerError("SMTP: сервер отклонил всех получателей")
    except smtplib.SMTPSenderRefused:
        raise MailerError("SMTP: сервер отклонил адрес отправителя")
    except smtplib.SMTPException as e:
        raise MailerError(f"SMTP: {type(e).__name__}")
    except (OSError, ssl.SSLError) as e:
        # Сюда попадают отказ в соединении, таймаут и несошедшийся
        # сертификат. Текст исключения тут безопасен (адрес хоста и порт
        # администратор и так видит в форме), а без него «не удалось
        # отправить» не даёт понять, что чинить.
        raise MailerError(f"SMTP: {type(e).__name__}: {e}")


def send_email(host: str, port: int, user: str, password: str, tls: str,
               sender: str, recipients_raw: str, subject: str, body: str,
               attachments: list[tuple[str, bytes, str]] | None = None) -> bool:
    """Собирает и отправляет письмо. False — почта не настроена (это не ошибка).

    Отсутствие хоста или получателей означает «уведомления по почте
    выключены»: §11 допускает работу без них, и алерт-путь не должен
    писать в лог ошибку на каждом событии из-за незаполненной формы.
    Отправитель по умолчанию равен логину — самый частый рабочий вариант,
    и он избавляет от обязательного к заполнению четвёртого поля.
    """
    recipients = split_recipients(recipients_raw)
    if not host or not recipients:
        return False
    if tls not in TLS_MODES:
        tls = "starttls"
    from_addr = sender or user or "facewatch@localhost"
    msg = build_message(from_addr, recipients, subject, body, attachments)
    send_message(msg, host, port, user, password, tls)
    return True
