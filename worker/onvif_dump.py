"""Диагностика ONVIF: снимает сырые SOAP-ответы камеры для разбора и тестов.

Зачем: логика выбора потоков, имени из OSD и адреса снимка написана по
спецификации ONVIF, но прошивки от неё отклоняются — именно так появилась
регрессия с выбором субпотока вместо основного (камера не вернула
разрешение в GetProfiles, чего код не ожидал). Реальные ответы конкретной
прошивки, сохранённые как фикстуры, ловят такие расхождения в CI навсегда,
в отличие от разовой проверки на живой камере.

Запуск (камера должна быть доступна из контейнера воркера):

    docker compose exec worker python /app/onvif_dump.py \\
        --host 192.168.105.19 --user admin --password 'ваш-пароль'

Учётные данные в выводе заменяются на ***: результат можно передавать
целиком, не вычищая его вручную и не рискуя забыть.
"""
import argparse
import re
import sys

import onvif_client as oc


def redact(text: str, username: str | None, password: str | None) -> str:
    """Убирает из вывода всё, по чему можно войти в камеру.

    Три разных места, где секрет успевает появиться: WS-Security-заголовок
    запроса, учётные данные внутри URI (их туда подставляет
    inject_credentials) и просто вхождение пароля в текст ответа.
    """
    out = text
    for secret in (password, username):
        if secret:
            out = out.replace(secret, "***")
    # user:pass@host в любых URI, даже если сам пароль в тексте не совпал
    # буквально (например, percent-кодированный).
    out = re.sub(r"(?<=://)[^/@\s]+:[^/@\s]+@", "***:***@", out)
    # PasswordDigest и Nonce выводятся от пароля — публиковать незачем.
    out = re.sub(r"(<[^>]*Password[^>]*>)[^<]*(</)", r"\1***\2", out)
    out = re.sub(r"(<[^>]*Nonce[^>]*>)[^<]*(</)", r"\1***\2", out)
    return out


def dump_raw(label: str, host: str, port: int, service: str, body: str,
             username: str | None, password: str | None) -> None:
    """Печатает сырой ответ на один SOAP-запрос."""
    url = f"http://{host}:{port}/onvif/{service}"
    print(f"\n{'=' * 70}\n### {label}\n{'=' * 70}")
    try:
        raw = oc._post(url, oc._soap_envelope(body, username, password), 6.0)
    except oc.OnvifError as e:
        print(f"ОШИБКА: {redact(str(e), username, password)}")
        return
    print(redact(raw.decode("utf-8", "replace"), username, password))


def main() -> int:
    ap = argparse.ArgumentParser(description="Снять ONVIF-ответы камеры")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    args = ap.parse_args()

    host, port, user, pw = args.host, args.port, args.user, args.password

    for label, service, body in (
        ("GetDeviceInformation", "device_service",
         f'<GetDeviceInformation xmlns="{oc._DEVICE_NS}"/>'),
        ("GetProfiles — определяет выбор основного потока и субпотока",
         "Media", f'<GetProfiles xmlns="{oc._MEDIA_NS}"/>'),
        ("GetOSDs — источник имени камеры", "Media",
         f'<GetOSDs xmlns="{oc._MEDIA_NS}"/>'),
    ):
        dump_raw(label, host, port, service, body, user, pw)

    # Разобранный результат: показывает, что из сырых ответов извлёк код.
    print(f"\n{'=' * 70}\n### Как это разобрал FaceWatch\n{'=' * 70}")
    try:
        profiles = oc.get_profiles(host, port, user, pw)
    except oc.OnvifError as e:
        print(f"GetProfiles не удался: {redact(str(e), user, pw)}")
        return 1

    for p in profiles:
        res = f"{p['width']}x{p['height']}" if p.get("width") else "разрешение НЕ сообщено"
        print(f"  профиль {p['token']:<24} {p['name']:<20} {res}")

    main_p, sub_p = oc.select_stream_profiles(profiles)
    print(f"\n  основной поток -> {main_p['name'] if main_p else '—'}")
    print(f"  субпоток       -> {sub_p['name'] if sub_p else '— (не будет использован)'}")

    osd = oc.get_osd_texts(host, port, user, pw)
    info = oc.get_device_information(host, port, user, pw)
    print(f"\n  тексты OSD: {osd or '— (GetOSDs не поддерживается или пуст)'}")
    print(f"  устройство: {info or '—'}")
    print(f"  предложенное имя камеры: "
          f"{oc.suggest_camera_name({'scopes': [], 'host': host}, info, osd)}")

    if main_p:
        for label, uri in (
            ("RTSP основного потока", oc.get_stream_uri(host, port, main_p["token"], user, pw)),
            ("HTTP-снимок (полное разрешение)",
             oc.get_snapshot_uri(host, port, main_p["token"], user, pw)),
        ):
            print(f"  {label}: {redact(str(uri), user, pw)}")

    print("\nВывод очищен от учётных данных — можно передавать целиком.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
