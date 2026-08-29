"""Опрос браузером ДЕЙСТВИЙ оператора, а не только открытия разделов.

`test_ui_live.py` проверяет, что раздел открывается и ни на что не
жалуется. Этого мало: страница может открыться чисто и всё равно не
делать того, ради чего она есть. Раздел «Отчёты» открывался чисто все 63
цикла — и всё это время отдавал CSV, который Excel под Windows показывал
кракозябрами (см. `services/csv_export.py`); открытие раздела об этом не
знало ничего, потому что кнопку никто не нажимал.

Отсюда правило набора: **каждая проверка совершает то действие, ради
которого функция описана в ТЗ, и смотрит на результат действия** —
скачанные байты, строку в БД, содержимое после перезагрузки. Не «кнопка
есть», не «запрос вернул 200».

Стенд — тот же, что у `test_ui_live.py` (настоящий nginx → бэкенд →
Postgres/Redis), фикстуры общие.
"""
import io
import pathlib
import zipfile

import pytest

BOM = b"\xef\xbb\xbf"


def _text_inputs(page):
    """Поля `<input>` без явного `type` — тоже текстовые.

    CSS-селектор `input[type=text]` их не находит: у разметки формы
    камеры тип не проставлен, и отбор по атрибуту возвращает пустой
    список. Отбор идёт по DOM-свойству.
    """
    return [i for i in page.query_selector_all("input")
            if i.evaluate("e=>e.type") == "text"]


@pytest.fixture()
def analytics_camera(logged_in):
    """Заводит камеру в режиме analytics ЧЕРЕЗ ФОРМУ и отдаёт её имя.

    Через форму, а не через API: §3 требует «добавление… через
    веб-интерфейс», и путь формы — это и есть проверяемое. Камера нужна
    ещё и редактору зон §15: список ROI показывает только analytics.
    """
    name = "E2E-Аналитика"
    logged_in.visit("/cameras")
    if name in logged_in.page.inner_text("body"):
        return name

    fields = _text_inputs(logged_in.page)
    assert len(fields) >= 2, "форма добавления камеры не открыта на /cameras"
    fields[0].fill(name)
    fields[1].fill("Корпус E2E")
    rtsp = logged_in.page.query_selector("input[placeholder^='rtsp://']")
    assert rtsp, "в форме нет поля RTSP-URL"
    rtsp.fill("rtsp://user:pass@127.0.0.1:554/main")
    for sel in logged_in.page.query_selector_all("select"):
        if "analytics" in sel.evaluate("e=>Array.from(e.options).map(o=>o.value)"):
            sel.select_option("analytics")
    logged_in.page.click("button:has-text('Добавить камеру') >> nth=-1")
    logged_in.page.wait_for_timeout(1500)

    assert name in logged_in.page.inner_text("body"), (
        f"§3: камера, заведённая через форму, не появилась в списке. "
        f"{logged_in.complaints()}"
    )
    return name


def test_camera_added_from_the_form_appears_in_the_list(analytics_camera, logged_in):
    """§3: «добавление IP-камер через веб-интерфейс».

    Проверка живёт в фикстуре (её результат нужен и зонам §15), здесь —
    её явное имя в отчёте прогона и перечитывание списка с нуля: камера
    должна пережить перезагрузку страницы, а не остаться в состоянии
    React.
    """
    logged_in.visit("/cameras")
    assert analytics_camera in logged_in.page.inner_text("body")
    assert not logged_in.complaints(), logged_in.complaints()


def test_roi_polygon_survives_a_reload(analytics_camera, logged_in, pg_conn):
    """§15: «веб-редактор полигонов» — нарисованная зона обязана сохраниться.

    Рисование идёт настоящими кликами по `<canvas>`: координаты полигона
    считаются из `getBoundingClientRect()`, и ошибка в этом пересчёте —
    единственный способ получить зону, которая сохранилась «не туда».
    Юнит-тест такую ошибку не увидит: canvas в нём не существует.

    Снимок кадра камеры в песочнице недоступен (воркер не поднят, снапшот
    отдаёт 404), и это ничего не портит: редактор рисует по серой
    подложке, а проверяется сохранение зоны, а не картинка под ней.
    """
    logged_in.visit("/roi")
    sel = logged_in.page.query_selector("select")
    options = sel.evaluate("e=>Array.from(e.options).map(o=>[o.value,o.text])")
    target = [v for v, t in options if t == analytics_camera]
    assert target, (
        f"§15: камеры analytics нет в списке редактора зон: {options}")
    cam_id = target[0]

    sel.select_option(cam_id)
    logged_in.page.wait_for_timeout(1200)
    canvas = logged_in.page.query_selector("canvas")
    assert canvas, "§15: на странице зон нет холста"
    box = canvas.bounding_box()

    # Редактор подгружает уже сохранённые зоны камеры, и новая рисуется
    # ВДОБАВОК к ним. Без сброса второй прогон набора нашёл бы два
    # полигона вместо одного — проверка была бы зелёной ровно один раз
    # на чистой базе, а в CI, где джоба идёт по существующему стенду,
    # падала бы «по непонятной причине».
    logged_in.page.click("button:has-text('Очистить все')")
    logged_in.page.wait_for_timeout(300)

    corners = [(100, 80), (300, 80), (300, 260), (100, 260)]
    for dx, dy in corners:
        logged_in.page.mouse.click(box["x"] + dx, box["y"] + dy)
        logged_in.page.wait_for_timeout(100)
    logged_in.page.click("button:has-text('Закрыть полигон')")
    logged_in.page.wait_for_timeout(300)
    logged_in.page.click("button:has-text('Сохранить')")
    logged_in.page.wait_for_timeout(1500)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT roi FROM cameras WHERE id = %s", (int(cam_id),))
        row = cur.fetchone()
    assert row and row[0], "§15: зона не доехала до БД"
    polygons = row[0].get("polygons") or []
    assert len(polygons) == 1, f"§15: ожидался один полигон, в БД {polygons!r}"
    assert len(polygons[0]) == len(corners), (
        f"§15: у сохранённого полигона {len(polygons[0])} вершин "
        f"вместо {len(corners)}")
    # Координаты нормированы в 0..1 — иначе зона, снятая на кадре 800×450,
    # не наложится на кадр камеры другого разрешения.
    for x, y in polygons[0]:
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0, (
            f"§15: координаты не нормированы: {polygons[0]!r}")


def test_report_downloads_are_real_files(logged_in):
    """§8: «Экспорт в Excel/CSV» — скачивается то, что откроется.

    Смотрим в БАЙТЫ скачанного, а не в код ответа. Причина конкретная:
    именно здесь 63 цикла жил CSV без BOM, который Excel под Windows
    показывал как «Ð’Ñ€ÐµÐ¼Ñ», — и все проверки того периода были
    зелёными, потому что читали ответ как текст (httpx снимает BOM сам)
    либо разбирали CSV, чей разбор одинаково успешен с BOM и без него.
    """
    logged_in.visit("/reports")

    with logged_in.page.expect_download(timeout=30000) as dl:
        logged_in.page.click("a.btn:has-text('Excel') >> nth=0")
    xlsx = pathlib.Path(dl.value.path()).read_bytes()
    assert xlsx[:2] == b"PK", f"§8: Excel-отчёт не zip-контейнер: {xlsx[:8]!r}"
    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        assert any(n.startswith("xl/worksheets/") for n in z.namelist()), (
            "§8: в xlsx нет ни одного листа")

    with logged_in.page.expect_download(timeout=30000) as dl:
        logged_in.page.click("a.btn:has-text('CSV') >> nth=0")
    csv_bytes = pathlib.Path(dl.value.path()).read_bytes()
    assert csv_bytes.startswith(BOM), (
        f"§8: CSV-отчёт скачан без BOM ({csv_bytes[:16]!r}) — Excel под "
        f"Windows прочитает его кириллические заголовки как cp1251")
    header = csv_bytes[len(BOM):].split(b"\n", 1)[0].decode("utf-8")
    assert "Время" in header or "ID" in header, (
        f"§8: неожиданный заголовок отчёта: {header!r}")


def test_audit_export_is_downloadable_and_marked(logged_in, base_url):
    """§10: «журнал аудита» выгружается — и той же кодировкой, что отчёты.

    Журнал скачивается ссылкой с токеном в query (заголовок к ссылке,
    которую открывает сам браузер, не прикрепить), поэтому запрос идёт
    из контекста страницы — с её токеном, а не голым клиентом.
    """
    logged_in.visit("/audit")
    body = logged_in.page.evaluate(
        """async () => {
            const t = localStorage.getItem('fw_token');
            const r = await fetch('/api/audit/export.csv?token=' + t);
            const b = new Uint8Array(await r.arrayBuffer());
            return {status: r.status, head: Array.from(b.slice(0, 8))};
        }"""
    )
    assert body["status"] == 200, f"§10: выгрузка журнала ответила {body['status']}"
    assert body["head"][:3] == [0xEF, 0xBB, 0xBF], (
        f"§10: журнал аудита выгружен без BOM: {body['head']!r}")
