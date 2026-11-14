import { describe, it, expect } from "vitest";
import {
  MAX_TAG_LEN,
  MAX_TAGS_PER_PERSON,
  addTag,
  normalizeTag,
  normalizeTags,
  parseTagInput,
  removeTag,
} from "./personTags";

describe("канонический вид тега", () => {
  it("не зависит от регистра и лишних пробелов", () => {
    // То же правило, что на сервере: иначе чип после перезагрузки
    // страницы меняет написание на глазах у оператора.
    expect(normalizeTag("  Подрядчик ")).toBe("подрядчик");
    expect(normalizeTag("ПОДРЯДЧИК")).toBe("подрядчик");
    expect(normalizeTag("служба   охраны")).toBe("служба охраны");
  });

  it("схлопывает написания одного тега в один", () => {
    expect(normalizeTags(["VIP", "vip", " Vip "])).toEqual(["vip"]);
  });

  it("идемпотентен", () => {
    const once = normalizeTags(["VIP", " склад "]);
    expect(normalizeTags(once)).toEqual(once);
  });
});

describe("разбор ввода", () => {
  it("делит по запятой и переводу строки, но не по пробелу", () => {
    // Пробел разделителем сделал бы из «служба охраны» два тега.
    expect(parseTagInput("склад, служба охраны\nvip")).toEqual(
      ["vip", "склад", "служба охраны"].sort((a, b) => a.localeCompare(b)),
    );
  });

  it("отбрасывает пустые куски ввода «a,,b»", () => {
    expect(parseTagInput("a,,b")).toEqual(["a", "b"]);
    expect(parseTagInput("   ")).toEqual([]);
  });
});

describe("добавление тега", () => {
  it("возвращает новый список, не мутируя старый", () => {
    const current = ["склад"];
    const r = addTag(current, "vip");
    expect(r.tags).toEqual(["vip", "склад"]);
    expect(current).toEqual(["склад"]);
  });

  it("повторное добавление того же тега в другом регистре ничего не плодит", () => {
    const r = addTag(["vip"], "VIP");
    expect(r.tags).toEqual(["vip"]);
  });

  it("добавляет несколько тегов из одной строки", () => {
    const r = addTag([], "vip, склад");
    expect(r.tags).toEqual(["vip", "склад"]);
  });

  it("отказывает с причиной, а не молча отбрасывает ввод", () => {
    // Молчаливый отказ здесь — тот же дефект, что «галочка watchlist
    // отскакивает обратно»: оператор уверен, что тег сохранён.
    const empty = addTag([], "   ");
    expect(empty.ok).toBe(false);

    const long = addTag([], "x".repeat(MAX_TAG_LEN + 1));
    expect(long.ok).toBe(false);
    expect(long.error).toContain(String(MAX_TAG_LEN));

    const many = addTag(
      Array.from({ length: MAX_TAGS_PER_PERSON }, (_, i) => `тег${i}`),
      "ещё-один",
    );
    expect(many.ok).toBe(false);
  });

  it("пропускает тег ровно на границе длины", () => {
    const r = addTag([], "x".repeat(MAX_TAG_LEN));
    expect(r.ok).toBe(true);
  });

  it("не считает переполнением дубль при полном списке", () => {
    // Список полон, но добавляемый тег в нём уже есть — итог не растёт,
    // и отказывать не за что.
    const full = Array.from({ length: MAX_TAGS_PER_PERSON }, (_, i) => `тег${i}`);
    const r = addTag(full, "ТЕГ0");
    expect(r.ok).toBe(true);
    expect(r.tags.length).toBe(MAX_TAGS_PER_PERSON);
  });
});

describe("снятие тега", () => {
  it("снимает независимо от регистра", () => {
    expect(removeTag(["vip", "склад"], "VIP")).toEqual(["склад"]);
  });

  it("на отсутствующем теге оставляет список как был", () => {
    expect(removeTag(["vip"], "нет-такого")).toEqual(["vip"]);
  });
});

describe("пределы совпадают с серверными", () => {
  it("40 символов и 20 тегов — как в backend/app/services/person_tags.py", () => {
    // Расхождение здесь означало бы, что клиент пропускает ввод, который
    // сервер отвергнет 400-м, — то есть отказ на сохранении вместо
    // подсказки при вводе.
    expect(MAX_TAG_LEN).toBe(40);
    expect(MAX_TAGS_PER_PERSON).toBe(20);
  });
});
