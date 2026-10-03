/**
 * Матрица прав §18 в интерфейсе.
 *
 * Тест читается рядом с таблицей §18 SPEC.md: каждая строка ожидания здесь
 * должна совпадать со строкой таблицы там. Смысл набора не в проверке
 * `Array.includes`, а в том, чтобы расхождение с ТЗ падало на CI, — до
 * цикла 51 матрица жила тремя копиями и разошлась незамеченной.
 */
import { describe, it, expect } from "vitest";
import { canAccess, canSeePersonIdentity, SECTION_ROLES, type Section } from "./access";

const ROLES = ["admin", "operator", "viewer"] as const;

describe("§18: разделы, закрытые наблюдателю", () => {
  // Каждый раздел модуля распознавания §15 закрыт наблюдателю: строки
  // «Поиск по фото», «Карточки персон», «Ручной апскейл лица» — все «Нет».
  const closedToViewer: Section[] = [
    "wall", "persons", "search", "roi", "archive", "reports",
    "monitoring", "cameras", "users", "settings", "audit",
  ];
  it.each(closedToViewer)("наблюдателю закрыт раздел %s", (section) => {
    expect(canAccess(section, "viewer")).toBe(false);
  });

  it("Стена распознавания закрыта наблюдателю — находка цикла 51", () => {
    // Регрессия, ради которой заведён этот файл: роут /wall был открыт
    // любой аутентифицированной роли, и наблюдатель видел имена, кадры
    // лиц и watchlist-разметку, получая при этом 403 на «Карточках персон».
    expect(canAccess("wall", "viewer")).toBe(false);
    expect(canAccess("wall", "operator")).toBe(true);
    expect(canAccess("wall", "admin")).toBe(true);
  });
});

describe("§18: что наблюдателю остаётся", () => {
  // Обратная сторона: §18 даёт наблюдателю две строки, и обе обязаны
  // работать. Фикс, закрывший бы их, нарушил бы матрицу так же, как её
  // нарушала прежняя выдача персон, — только в другую сторону.
  it.each(["dashboard", "live", "profile"] as Section[])(
    "наблюдателю открыт раздел %s", (section) => {
      expect(canAccess(section, "viewer")).toBe(true);
    });

  it("живой просмотр §4 остаётся у всех ролей", () => {
    // §4 «bounding box'ы вокруг лиц с подписями» + строка «Просмотр видео
    // онлайн: Да/Да/Да». Именно поэтому /ws/faces наблюдателю не
    // закрывается, а фильтруется по полям (backend: face_feed_acl.py).
    for (const role of ROLES) expect(canAccess("live", role)).toBe(true);
  });
});

describe("§18: разделы только для администратора", () => {
  it.each(["cameras", "users", "settings", "audit"] as Section[])(
    "оператору закрыт раздел %s", (section) => {
      expect(canAccess(section, "operator")).toBe(false);
      expect(canAccess(section, "admin")).toBe(true);
    });
});

describe("данные карточек персон", () => {
  it("блок «Топ-10 персон» дашборда — только ролям карточек", () => {
    expect(canSeePersonIdentity("viewer")).toBe(false);
    expect(canSeePersonIdentity("operator")).toBe(true);
    expect(canSeePersonIdentity("admin")).toBe(true);
  });

  it("неизвестная роль трактуется как наименее привилегированная", () => {
    // Роль приходит из localStorage; пустая строка (не залогинен) и любое
    // будущее значение не должны открывать карточки по умолчанию.
    for (const role of ["", "guest", "какая-то-новая-роль"]) {
      expect(canSeePersonIdentity(role)).toBe(false);
      expect(canAccess("wall", role)).toBe(false);
    }
  });
});

describe("целостность таблицы", () => {
  it("в каждой строке есть администратор", () => {
    // §18 не содержит ни одного действия, запрещённого администратору.
    for (const section of Object.keys(SECTION_ROLES) as Section[]) {
      expect(canAccess(section, "admin")).toBe(true);
    }
  });
});
