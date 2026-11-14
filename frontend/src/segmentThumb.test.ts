/**
 * Адрес миниатюры кадра сегмента архива (SPEC §7).
 *
 * Проверяется одно свойство: **адрес постоянен во времени**. Соседний
 * `camSnapshotUrl` намеренно добавляет `Date.now()` — снимок камеры живой
 * и обязан перезапрашиваться. Скопировать эту строку сюда легко, и
 * сломается от этого не картинка, а сервер: адрес, меняющийся на каждый
 * рендер, сбивает кэш браузера, и выдача из 200 строк заново тянет 200
 * миниатюр при каждом уточнении фильтра — а каждая непрогретая миниатюра
 * это вызов ffmpeg. Регрессия невидима в браузере: картинки на месте.
 */
import { beforeEach, describe, expect, it } from "vitest";

// vitest в этом проекте гоняется в окружении node (см. vite.config.ts —
// jsdom не подключён, остальные тесты проверяют чистые модули). `api.ts`
// читает токен из localStorage, которого в node нет, поэтому минимальная
// заглушка ставится ДО импорта модуля: он читает хранилище на вызове, но
// сам импорт тоже не должен падать.
const store = new Map<string, string>();
(globalThis as any).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

const { segmentThumbUrl } = await import("./api");

beforeEach(() => {
  store.clear();
  store.set("fw_token", "test-token");
});

describe("segmentThumbUrl", () => {
  it("указывает на эндпоинт архива с id сегмента", () => {
    expect(segmentThumbUrl(42)).toContain("/api/archive/thumb/42");
  });

  it("несёт токен в query string", () => {
    // Адрес подставляется в <img src>, заголовок к нему не прикрепить —
    // ровно как у скачивания сегмента и экспорта фрагмента.
    expect(segmentThumbUrl(42)).toContain("token=test-token");
  });

  it("не меняется между вызовами", () => {
    const first = segmentThumbUrl(7);
    const second = segmentThumbUrl(7);
    expect(second).toBe(first);
  });

  it("не содержит сбивающей кэш метки времени", () => {
    // Прямая проверка на случай, если метку добавят под другим именем:
    // в адресе не должно быть ничего похожего на unix-миллисекунды.
    expect(segmentThumbUrl(7)).not.toMatch(/\d{13}/);
  });

  it("различает сегменты", () => {
    expect(segmentThumbUrl(1)).not.toBe(segmentThumbUrl(2));
  });
});
