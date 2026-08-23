import { describe, expect, it } from "vitest";
import { humanDuration, modelBanner } from "./modelLoad";

describe("humanDuration", () => {
  it("секунды до полутора минут", () => {
    expect(humanDuration(0)).toBe("0 с");
    expect(humanDuration(41.4)).toBe("41 с");
    expect(humanDuration(89)).toBe("89 с");
  });

  it("дальше минуты — они читаются, а не считаются", () => {
    expect(humanDuration(90)).toBe("2 мин");
    expect(humanDuration(600)).toBe("10 мин");
  });

  it("отрицательное время не показывается как отрицательное", () => {
    // Разность часов на клиенте и воркере теоретически может дать минус;
    // «-3 с» на странице мониторинга выглядело бы поломкой.
    expect(humanDuration(-5)).toBe("0 с");
  });
});

describe("modelBanner", () => {
  it("готовая модель баннера не даёт", () => {
    expect(modelBanner({ model_ready: true, model: "buffalo_s" })).toBeNull();
  });

  it("отсутствие данных о слое аналитики — не авария", () => {
    // Redis пуст либо воркер ещё не опубликовал состояние. Рисовать здесь
    // «модель не загружена» значило бы объявить аварию по незнанию.
    expect(modelBanner(null)).toBeNull();
    expect(modelBanner(undefined)).toBeNull();
  });

  it("идущая загрузка — не авария, а нейтральная строка", () => {
    const b = modelBanner({
      model_ready: false, model: "buffalo_s",
      load: { state: "loading", seconds: 41, model: "buffalo_s" },
    })!;
    expect(b.tone).toBe("info");
    expect(b.title).toContain("buffalo_s");
    expect(b.title).toContain("41 с");
    expect(b.detail).toContain("Запись идёт");
  });

  it("короткая загрузка не советует чинить сеть", () => {
    // Первые секунды загрузка идёт всегда и на исправном сервере: совет
    // «положите модель вручную» в этот момент — ложная тревога.
    const b = modelBanner({
      model_ready: false, load: { state: "loading", seconds: 12 },
    })!;
    expect(b.hint).toBeNull();
  });

  it("затянувшаяся загрузка подсказывает про изолированный сервер", () => {
    const b = modelBanner({
      model_ready: false, load: { state: "loading", seconds: 300 },
    })!;
    expect(b.hint).toContain("insightface-models");
  });

  it("отказ остаётся аварией с причиной", () => {
    const b = modelBanner({
      model_ready: false, model: "buffalo_s",
      load: { state: "error", seconds: 3, model: "buffalo_s",
              error: "ConnectionError: нет сети" },
    })!;
    expect(b.tone).toBe("warn");
    expect(b.title).toContain("не загружена");
    expect(b.detail).toBe("ConnectionError: нет сети");
    expect(b.hint).toContain("insightface-models");
  });

  it("воркер без поля load показывает прежнюю аварию", () => {
    // Совместимость вниз: обновлён бэкенд, воркер ещё старый. Пропажа
    // сообщения об отказе была бы регрессом §9, а не косметикой.
    const b = modelBanner({
      model_ready: false, model: "buffalo_s", error: "нет сети",
    })!;
    expect(b.tone).toBe("warn");
    expect(b.detail).toBe("нет сети");
  });

  it("смена профиля называет новую модель, а не прежнюю", () => {
    // Администратор только что переключил профиль: строка обязана
    // относиться к тому, что грузится сейчас.
    const b = modelBanner({
      model_ready: false, model: "buffalo_s",
      load: { state: "loading", seconds: 8, model: "buffalo_l" },
    })!;
    expect(b.title).toContain("buffalo_l");
    expect(b.title).not.toContain("buffalo_s");
  });
});
