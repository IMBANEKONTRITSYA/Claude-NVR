"""Сверка двух копий раскладки миниатюр: backend/app/services/thumbs.py и
worker/thumbs.py (ТЗ §7).

Режет миниатюры бэкенд, а удаляет их воркер при ротации архива (§5) — он
один владеет удалением сегментов. Зависимости от пакета `app` у воркера
нет, поэтому вычисление пути продублировано; тот же случай, что и с
`mailer` (test_mailer_parity.py) и ORM-моделями (test_worker_models.py).

Класс регрессии, который ловит этот тест: правка раскладки в бэкенде не
доезжает до воркера. Расходятся копии **молча** — ротация продолжает
удалять файлы, просто по несуществующим путям, миниатюры переживают свои
сегменты, и видно это на объекте только как медленно растущий диск.
Поэтому сверяется не только поведение на числах, но и тело функции: копия,
переписанная «эквивалентно», завтра разойдётся с оригиналом на следующей
правке.
"""
import ast
import importlib.util
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
BACKEND_THUMBS = os.path.join(ROOT, "backend", "app", "services", "thumbs.py")
WORKER_THUMBS = os.path.join(ROOT, "worker", "thumbs.py")

# Имена, обязанные совпадать. Всё остальное у копий своё: бэкенд режет
# кадр (ffmpeg, семафор, кэш), воркер — только удаляет.
SHARED_NAMES = {"THUMB_DIR", "thumb_rel_path", "thumb_path"}


def _definitions(path: str) -> dict[str, ast.AST]:
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node
    return out


def _strip_docstring(node: ast.AST) -> ast.AST:
    """Докстринги у копий разные осознанно — сверяется код, а не текст."""
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.body:
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            node = ast.parse(ast.unparse(node)).body[0]
            node.body = node.body[1:]
    return node


def test_shared_definitions_present_in_both():
    backend = _definitions(BACKEND_THUMBS)
    worker = _definitions(WORKER_THUMBS)
    assert SHARED_NAMES <= set(backend), "в бэкенде пропало общее определение"
    assert SHARED_NAMES <= set(worker), "в воркер не доехало общее определение"


def test_shared_definitions_identical():
    backend = _definitions(BACKEND_THUMBS)
    worker = _definitions(WORKER_THUMBS)
    for name in sorted(SHARED_NAMES):
        assert ast.dump(_strip_docstring(backend[name])) == \
               ast.dump(_strip_docstring(worker[name])), \
               f"копии {name} разошлись"


def _load_worker_thumbs():
    spec = importlib.util.spec_from_file_location("worker_thumbs", WORKER_THUMBS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paths_match_on_real_ids():
    """Поведенческая сверка поверх сверки по AST.

    AST ловит расхождение кода, эта проверка — расхождение результата на
    краях (id 0, граница шарда, номер за пределами 500 000 из §7).
    """
    from app.services import thumbs as backend_thumbs
    worker_thumbs = _load_worker_thumbs()

    for seg_id in (0, 1, 999, 1000, 1001, 499_999, 500_000, 1_234_567):
        assert backend_thumbs.thumb_rel_path(seg_id) == worker_thumbs.thumb_rel_path(seg_id)
        assert backend_thumbs.thumb_path("/media", seg_id) == \
               worker_thumbs.thumb_path("/media", seg_id)


def test_worker_drop_thumb_removes_only_its_own(tmp_path):
    """Ротация удаляет миниатюру своего сегмента и не трогает соседние."""
    worker_thumbs = _load_worker_thumbs()
    media = str(tmp_path)
    for seg_id in (5, 6):
        path = worker_thumbs.thumb_path(media, seg_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8jpeg")

    assert worker_thumbs.drop_thumb(media, 5) is True
    assert not os.path.exists(worker_thumbs.thumb_path(media, 5))
    assert os.path.exists(worker_thumbs.thumb_path(media, 6))


def test_worker_drop_thumb_tolerates_missing_file(tmp_path):
    """Отсутствие миниатюры — норма, а не ошибка ротации.

    Миниатюра появляется только у сегментов, которые кто-то открывал в
    выдаче, а под ротацию попадают все подряд: исключение здесь останавливало
    бы очистку архива на первом же неоткрытом сегменте.
    """
    worker_thumbs = _load_worker_thumbs()
    assert worker_thumbs.drop_thumb(str(tmp_path), 12345) is False
