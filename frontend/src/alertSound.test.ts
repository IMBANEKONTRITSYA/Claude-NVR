import { describe, expect, it } from "vitest";
import { BEEP_THROTTLE_MS, isAlertEvent, shouldBeep } from "./alertSound";

describe("звуковой алерт §6: какие события озвучиваются", () => {
  it("сигналит только по watchlist-персоне", () => {
    expect(isAlertEvent({ type: "face", alert: true })).toBe(true);
  });

  it("молчит на обычном событии распознавания", () => {
    // На объекте в час проходят сотни лиц: если бы сигналило каждое,
    // оператор выключил бы звук в первый же день и не услышал бы алерт.
    expect(isAlertEvent({ type: "face", alert: false })).toBe(false);
    expect(isAlertEvent({ type: "face" })).toBe(false);
  });

  it("молчит на служебных сообщениях ленты", () => {
    // type=box идёт на каждый кадр живой детекции (до 5 раз в секунду
    // на камеру), type=enhanced — подмена фото после апскейла.
    expect(isAlertEvent({ type: "box", alert: true })).toBe(false);
    expect(isAlertEvent({ type: "enhanced", event_id: 1 })).toBe(false);
  });

  it("не падает на пустом сообщении", () => {
    expect(isAlertEvent(null)).toBe(false);
    expect(isAlertEvent(undefined)).toBe(false);
  });
});

describe("тротлинг сигнала", () => {
  it("первый сигнал проходит", () => {
    expect(shouldBeep(0, 1_000_000)).toBe(true);
  });

  it("подряд идущие watchlist-персоны не накладывают гудки друг на друга", () => {
    // Кулдаун воркера считается по персоне, поэтому пять разных персон в
    // одну секунду — пять законных алертов; здесь их сводит в один сигнал.
    const t = 1_000_000;
    expect(shouldBeep(t, t + 100)).toBe(false);
    expect(shouldBeep(t, t + BEEP_THROTTLE_MS - 1)).toBe(false);
  });

  it("после истечения интервала сигнал снова проходит", () => {
    const t = 1_000_000;
    expect(shouldBeep(t, t + BEEP_THROTTLE_MS)).toBe(true);
  });
});
