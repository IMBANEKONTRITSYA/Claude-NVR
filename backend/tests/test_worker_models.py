"""Статическая проверка ORM-моделей воркера.

Воркер объявляет свои копии таблиц (у него нет зависимости от app.models), поэтому
поле, добавленное только в бэкенде, легко забыть продублировать. SQLAlchemy на
неизвестный kwarg бросает TypeError уже в рантайме — нить камеры падает,
события не пишутся. Тест разбирает worker.py через ast, без импорта cv2/insightface.
"""
import ast
import os

WORKER = os.path.join(os.path.dirname(__file__), "..", "..", "worker", "worker.py")


def _parse():
    with open(WORKER, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _model_columns(tree) -> dict[str, set[str]]:
    """{имя класса модели: набор объявленных колонок}"""
    models = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cols = {
            stmt.targets[0].id
            for stmt in node.body
            if isinstance(stmt, ast.Assign)
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
            and getattr(stmt.value.func, "id", None) == "Column"
        }
        if cols:
            models[node.name] = cols
    return models


def test_worker_model_kwargs_are_declared():
    tree = _parse()
    models = _model_columns(tree)
    assert "FaceEvent" in models, "В worker.py не найдена модель FaceEvent"

    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        cols = models.get(node.func.id)
        if cols is None:
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg not in cols:
                problems.append(f"{node.func.id}(...) получает '{kw.arg}', которого нет среди колонок")

    assert not problems, "Несогласованность моделей воркера:\n" + "\n".join(problems)


def test_face_event_has_upscaler_columns():
    # Колонки, от которых зависит пайплайн апскейла
    cols = _model_columns(_parse())["FaceEvent"]
    for required in ("orig_snapshot_path", "enhanced"):
        assert required in cols, f"FaceEvent в воркере без колонки {required}"
