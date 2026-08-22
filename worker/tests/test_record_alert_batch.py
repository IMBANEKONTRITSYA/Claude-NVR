"""§9 «Алерты: потеря потока, пропуск записи» — решение о сообщении.

Из трёх алертов раздела оповещение уходило только по диску. Потеря потока
и пропуск записи писались в лог и красились в интерфейсе «Мониторинга» —
то есть доходили лишь до того, кто в эту минуту смотрит на страницу. Довод,
по которому диску оповещение сделали, записан прямо в коде цикла 29:
«журнал на объекте никто не читает, пока архив не начал стираться». К этим
двум он приложим сильнее: камера, переставшая писаться ночью, не оставляет
по себе ничего, кроме дыры в архиве, и находят её в день, когда запись
понадобилась.

**Набор живёт здесь, а не рядом с worker.py, намеренно.** `worker.py`
тянет cv2/insightface/onnxruntime, и всё, что его импортирует, в лёгкой
CI-джобе воркера уходит в skip. Проверка, которая гоняется только в
песочнице, повторила бы ровно тот дефект, который этим PR закрывается:
выглядит рабочей, а в CI не выполняется ни разу. Поэтому решение «о чём и
как сообщить» вынесено в `record_status.py` (только stdlib), а сетевое —
кулдаун через Redis и сама отправка — осталось в воркере и проверяется
`test_record_layer_alerts.py` с `importorskip`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from record_status import ALERT_IDS_SHOWN, RECORD_ALERTS, alert_batch  # noqa: E402

ALWAYS = lambda _cid: True          # noqa: E731 — кулдаун истёк по всем
NEVER = lambda _cid: False          # noqa: E731 — все в кулдауне


def test_mass_outage_yields_one_message_not_one_per_camera():
    """Моргнувший коммутатор роняет весь объект в одном проходе.

    Сообщение на каждую камеру означало бы сто двадцать писем подряд — так
    алертинг и выключают, после чего следующий, настоящий, не читает никто.
    """
    cams = list(range(1, 121))

    fresh, subject, text = alert_batch("stream_lost", cams, ALWAYS)

    assert fresh == cams
    assert "камер 120" in text
    assert subject == RECORD_ALERTS["stream_lost"][1]


def test_long_list_is_truncated_with_a_counter():
    """Полный список нечитаем в письме, а Telegram обрежет его сам — молча
    и посередине. Обрезаем сами, оставляя счётчик остатка."""
    fresh, _subject, text = alert_batch("stream_lost", list(range(1, 121)), ALWAYS)

    assert f"#{ALERT_IDS_SHOWN}," not in text, "показан лишний номер"
    assert f"и ещё {120 - ALERT_IDS_SHOWN}" in text
    assert text.count("#") == ALERT_IDS_SHOWN


def test_short_list_is_shown_whole_without_a_counter():
    """Позитивный контроль к предыдущему: без него усечение прошло бы и на
    коде, который дописывает «и ещё 0» к любому списку."""
    _fresh, _subject, text = alert_batch("segment_missing", [4, 9], ALWAYS)

    assert "#4, #9" in text
    assert "и ещё" not in text


def test_cooldown_filters_cameras_not_the_whole_batch():
    """Камера в кулдауне выпадает, соседняя — нет.

    Отсев «всё или ничего» проглотил бы вторую камеру, отвалившуюся следом
    за первой, а это развитие аварии, ради которого алерт и заведён.
    """
    fresh, _subject, text = alert_batch("stream_lost", [1, 2, 3],
                                        lambda cid: cid != 2)

    assert fresh == [1, 3]
    assert "камер 2" in text
    assert "#2" not in text


def test_nothing_to_say_returns_none():
    """Все камеры в кулдауне — вызывающий не должен слать пустое письмо."""
    assert alert_batch("stream_lost", [1, 2], NEVER) is None
    assert alert_batch("stream_lost", [], ALWAYS) is None


def test_kinds_have_their_own_wording():
    """«Поток потерян» и «запись не идёт» — разные отказы.

    Один текст на оба означал бы, что дежурный едет проверять сеть, когда
    на сервере кончилось место, и наоборот.
    """
    _f1, subj_lost, text_lost = alert_batch("stream_lost", [1], ALWAYS)
    _f2, subj_gap, text_gap = alert_batch("segment_missing", [1], ALWAYS)

    assert subj_lost != subj_gap
    assert text_lost != text_gap
    assert "поток" in text_lost.lower()
    assert "запис" in text_gap.lower()


def test_every_kind_used_by_the_worker_has_wording():
    """Новый вид алерта без ярлыка упал бы KeyError уже на объекте — в
    проходе менеджера, то есть в момент самой аварии."""
    for kind, value in RECORD_ALERTS.items():
        what, subject = value
        assert what and subject, kind
        assert alert_batch(kind, [1], ALWAYS) is not None
