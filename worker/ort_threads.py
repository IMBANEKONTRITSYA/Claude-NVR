"""Бюджет потоков ONNX Runtime для слоя аналитики (SPEC §16, §19).

§16 отводит камере analytics «0.5-1.5 ядра (5 FPS)», §19 требует «CPU ≤
80 % при полной нагрузке», §2 — чтобы аналитика не влияла на запись. Все
три требования предполагают, что у слоя аналитики есть потолок по CPU. До
цикла 36 его не было ни в каком виде: `load_face_app()` создавал сессии
ONNX Runtime с настройками по умолчанию, а по умолчанию ORT заводит пул
intra-op потоков по числу ядер машины.

**Измеренная цена отсутствия потолка.** Песочница, 4 ядра, `buffalo_s`,
det_size 640, 6 лиц в кадре, нити камер дёргают общий `FACE_APP` — то есть
ровно так, как устроен воркер:

| Потоков на камеру | Камер | Суммарно FPS | На камеру | Занято ядер | FPS/ядро |
|---|---|---|---|---|---|
| без ограничения | 1 | 12.21 | 12.21 | 3.95 | 3.09 |
| без ограничения | 2 | 14.28 | 7.14 | 3.94 | 3.63 |
| без ограничения | 4 | 18.51 | **4.63** | 3.95 | 4.69 |
| 1 | 1 | 9.75 | 9.75 | **1.00** | 9.75 |
| 1 | 2 | 19.43 | 9.71 | 1.99 | 9.75 |
| 1 | 4 | **41.09** | **10.27** | 3.90 | **10.54** |
| 2 | 2 | 24.07 | 12.04 | 3.54 | 6.79 |

Два вывода. Первый: без потолка **одна** камера занимает машину целиком, и
масштабирование внутри инференса отвратительное — 3.09 FPS на ядро против
9.75 при одном потоке. Второй, важнее: на четырёх камерах аналитики без
потолка выходит **4.63 FPS на канал — ниже норматива §19 «≥ 5 FPS»**, а с
потолком в один поток на камеру — 10.27 FPS на том же железе, вдвое выше
норматива. Ограничение здесь не «отдать производительность за
предсказуемость», а прямой выигрыш в 2.2 раза.

На целевом сервере §20 (64 потока) пул по умолчанию был бы на 64 потока со
той же эффективностью, и слой записи (120 remux-потоков MediaMTX)
соревновался бы за CPU с аналитикой вопреки §2.

**Почему не переменными окружения.** Сборки ONNX Runtime с 1.16
используют собственный пул потоков вместо OpenMP и читают только
`SessionOptions.intra_op_num_threads`; `OMP_NUM_THREADS` на них не влияет
(проверено: с выставленным `OMP_NUM_THREADS=1` занято 3.97 ядра).

**Почему потребовался патч класса сессии.** `FaceAnalysis(**kwargs)`
пробрасывает лишние аргументы вниз, но `insightface.model_zoo.get_model()`
на последнем шаге вызывает `router.get_model(providers=...,
provider_options=...)` и `sess_options` теряет. Другого способа задать
опции сессии insightface не предлагает, поэтому подменяется класс
`PickableInferenceSession`, через который проходит создание каждой сессии.
Подмена идемпотентна, снимается `restore()`, и если insightface изменится —
`limit_threads()` вернёт False, что видно в логе загрузки модели
(`ort_threads_applied`), а не молча.

**О смысле числа.** `intra_op_num_threads` — потолок на ОДИН вызов
инференса, а нити камер вызывают его одновременно на общей сессии. То есть
это бюджет НА КАМЕРУ, и слой в сумме занимает примерно
`камеры × потоки` ядер. Отсюда и арифметика ниже.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("facewatch.worker")

# Потолок потоков на камеру при автоматическом расчёте. Замер выше: один
# поток даёт ~9.7 FPS на канал (вдвое выше норматива §19) при ровно одном
# занятом ядре; второй поток добавляет ~24 % FPS за +80 % CPU. Больше двух
# не рассматривается вовсе — при трёх и выше эффективность падает ниже
# половины, и те же ядра выгоднее отдать другой камере.
MAX_THREADS_PER_CAMERA = 2

# Доля машины, выше которой слой аналитики не поднимается. §19 оставляет
# 20 % запаса, а из остального слою записи нужно 0.04 ядра на камеру (§16)
# плюс приложение (§2). Половина — грубая, но безопасная граница:
# аналитика перестаёт быть причиной, по которой встанет запись.
MAX_MACHINE_SHARE = 0.5

_original_session = None


def analytics_thread_budget(cores: int, analytics_cameras: int,
                            configured: int = 0) -> int:
    """Сколько потоков ORT отдать одной камере analytics.

    `configured > 0` — значение из настроек, оно главнее любой арифметики:
    администратор объекта видит фактический FPS по камерам в «Мониторинге»
    и вправе подобрать число под своё железо (в том числе поднять его выше
    расчётного, если камер аналитики одна, а ядер много).

    Автоматический расчёт делит отведённую слою половину машины на число
    разрешённых камер analytics и режет результат по
    `MAX_THREADS_PER_CAMERA`. Минимум — один поток: ноль означал бы
    «ORT решает сам», то есть возврат к пулу по числу ядер.
    """
    cores = max(1, int(cores or 1))
    if configured and configured > 0:
        # Потолок машины применяется и к ручному значению: пул больше числа
        # ядер не ускоряет ничего, а планировщику добавляет работы.
        return max(1, min(int(configured), cores))
    cameras = max(1, int(analytics_cameras or 1))
    layer_cores = max(1, int(cores * MAX_MACHINE_SHARE))
    return max(1, min(MAX_THREADS_PER_CAMERA, layer_cores // cameras))


def limit_threads(threads: int) -> bool:
    """Ограничить пул ORT `threads` потоками. True, если подмена встала.

    Вызывать ДО создания сессий (то есть до `FaceAnalysis(...)`): опции
    читаются в момент создания, у существующей сессии пул уже свой.
    """
    global _original_session
    try:
        import onnxruntime
        from insightface.model_zoo import model_zoo as mz
    except ImportError as exc:
        logger.warning("не удалось ограничить потоки ONNX Runtime: нет %s",
                       getattr(exc, "name", exc))
        return False

    base = _original_session or mz.PickableInferenceSession
    _original_session = base

    class _Limited(base):  # type: ignore[misc, valid-type]
        def __init__(self, model_path, **kwargs):
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = threads
            # Межоперационный параллелизм при последовательном графе не
            # даёт ничего, кроме ещё одного пула потоков.
            options.inter_op_num_threads = 1
            kwargs["sess_options"] = options
            super().__init__(model_path, **kwargs)

    mz.PickableInferenceSession = _Limited
    # BLAS внутри numpy и сборки ORT с OpenMP читают эти переменные. На пул
    # ORT в современных сборках они не влияют (см. docstring), но закрывают
    # тот же вопрос вторым способом там, где влияют. `setdefault` — чтобы не
    # перебивать значение, выставленное в окружении контейнера.
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, str(threads))
    return True


def restore() -> None:
    """Вернуть исходный класс сессии (для тестов)."""
    global _original_session
    if _original_session is None:
        return
    from insightface.model_zoo import model_zoo as mz
    mz.PickableInferenceSession = _original_session
    _original_session = None
