"""Набор модулей insightface, поднимаемых воркером (SPEC §19).

FaceAnalysis по умолчанию поднимает пять моделей и прогоняет четыре из них
НА КАЖДОЕ ЛИЦО КАЖДОГО кадра: detection, landmark_3d_68, landmark_2d_106,
genderage, recognition. FaceWatch читает из результата ровно два поля —
`bbox` и `normed_embedding`. Замерено (buffalo_s, det_size 640, один поток,
кадр с шестью лицами): 723 мс/кадр против 112 мс/кадр, шестикратная
разница на самом дорогом шаге слоя аналитики.

Регрессия здесь безболезненна на вид и невидима в тестах: убрать
`allowed_modules` — значит вернуть корректную работу системы, просто в
шесть раз медленнее. На объекте это разница между «укладываемся в §19» и
«не укладываемся», а в песочнице без реальной модели не видно вовсе.
Поэтому проверка статическая (ast) — она выполняется в CI, где cv2 нет.
"""
import ast
import os

WORKER = os.path.join(os.path.dirname(__file__), "..", "worker.py")

# Поля объекта Face, которых НЕ будет при суженном наборе модулей.
# Появление любого из них в worker.py означает, что кто-то добавил функцию,
# которой нужен выключенный модуль, — и она молча получит None/AttributeError.
ATTRS_REQUIRING_DISABLED_MODULES = {
    "age", "sex", "gender", "landmark_2d_106", "landmark_3d_68", "pose",
}


def _tree():
    with open(WORKER, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _face_analysis_call(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FaceAnalysis":
            return node
    return None


def test_face_analysis_is_constructed_with_restricted_modules():
    call = _face_analysis_call(_tree())
    assert call is not None, "вызов FaceAnalysis(...) в worker.py не найден"
    kwargs = {kw.arg for kw in call.keywords}
    assert "allowed_modules" in kwargs, (
        "FaceAnalysis без allowed_modules поднимает landmark_3d_68, "
        "landmark_2d_106 и genderage и прогоняет их на каждое лицо каждого "
        "кадра — цепочка §19 замедляется в ~6 раз, оставаясь корректной"
    )


def test_module_list_covers_exactly_what_the_worker_reads():
    """Набор ровно из двух модулей: детекция (bbox) и распознавание
    (эмбеддинг). Расширение списка — осознанное решение с ценой в FPS."""
    tree = _tree()
    modules = None
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", None) == "FACE_MODULES"):
            modules = ast.literal_eval(node.value)
    assert modules is not None, "FACE_MODULES не объявлен в worker.py"
    assert set(modules) == {"detection", "recognition"}, modules


def test_worker_does_not_read_attributes_of_disabled_models():
    """Если кто-то начнёт читать возраст/пол/лицевые точки, он получит их
    невычисленными — а не ошибку. Такой признак сам себя не проявит."""
    used = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Attribute) and node.attr in ATTRS_REQUIRING_DISABLED_MODULES:
            used.add(node.attr)
    assert not used, (
        f"worker.py читает {sorted(used)} — эти поля не вычисляются при "
        f"FACE_MODULES=['detection', 'recognition']. Либо признак не нужен, "
        f"либо модуль надо вернуть в список и принять цену в FPS (§19)."
    )
