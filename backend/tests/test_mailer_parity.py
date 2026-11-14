"""Сверка двух копий SMTP-транспорта: backend/app/services/mailer.py и
worker/mailer.py.

У воркера нет зависимости от пакета `app` (см. test_worker_models.py — та же
причина заставляет его держать собственные копии ORM-моделей), поэтому
транспорт продублирован. Класс регрессии, который ловит этот тест, в проекте
уже случался с моделями: правка в бэкенде не доезжает до воркера, и расходятся
они молча — алерт по почте перестаёт уходить только на боевом объекте.

Сверяется поведение (тела функций и значения констант через ast.dump), а не
текст файла: разные докстринги модулей и порядок комментариев допустимы,
разная логика отправки — нет.
"""
import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
BACKEND_MAILER = os.path.join(ROOT, "backend", "app", "services", "mailer.py")
WORKER_MAILER = os.path.join(ROOT, "worker", "mailer.py")

# Всё, что обязано совпадать. Функция, добавленная в одну копию и не
# добавленная в другую, роняет тест на проверке набора имён ниже, поэтому
# список не надо поддерживать вручную при расширении модуля.
SHARED_NAMES = {
    "TLS_MODES", "SMTP_TIMEOUT_SEC", "MailerError",
    "split_recipients", "build_message", "send_message", "send_email",
}


def _definitions(path: str) -> dict[str, ast.AST]:
    """{имя верхнего уровня: узел определения} — присваивания, функции, классы."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node
    return out


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Убирает докстринги: они поясняют модуль, а не поведение, и в копии
    воркера законно отличаются ссылкой на оригинал."""
    clone = ast.parse(ast.unparse(node)).body[0]
    for sub in ast.walk(clone):
        if isinstance(sub, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            if (sub.body and isinstance(sub.body[0], ast.Expr)
                    and isinstance(sub.body[0].value, ast.Constant)
                    and isinstance(sub.body[0].value.value, str)):
                sub.body.pop(0)
                if not sub.body:
                    sub.body.append(ast.Pass())
    return clone


def test_worker_copy_defines_the_same_names():
    backend = _definitions(BACKEND_MAILER)
    worker = _definitions(WORKER_MAILER)
    assert SHARED_NAMES <= set(backend), "в backend-копии пропало общее имя"
    assert set(backend) == set(worker), (
        "набор определений разошёлся: "
        f"только в backend — {sorted(set(backend) - set(worker))}, "
        f"только в worker — {sorted(set(worker) - set(backend))}"
    )


def test_shared_definitions_are_identical():
    backend = _definitions(BACKEND_MAILER)
    worker = _definitions(WORKER_MAILER)
    for name in sorted(SHARED_NAMES):
        a = ast.dump(_strip_docstrings(backend[name]))
        b = ast.dump(_strip_docstrings(worker[name]))
        assert a == b, (
            f"{name} расходится между backend/app/services/mailer.py и "
            f"worker/mailer.py — правку надо продублировать в обе копии"
        )


def test_worker_copy_imports_only_stdlib():
    """Копия воркера не должна тянуть пакет app или сторонние зависимости —
    иначе она не импортируется в контейнере воркера вовсе."""
    with open(WORKER_MAILER, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    allowed = {"smtplib", "ssl", "email"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "относительный импорт не сработает в воркере"
            assert (node.module or "").split(".")[0] in allowed, node.module
