/**
 * Тесты представления FPS и битрейта (SPEC §9).
 *
 * §9 перечисляет содержимое статуса потоков поимённо — «онлайн/оффлайн,
 * FPS, битрейт», — и слой записи отдавал из трёх величин одну. Здесь
 * проверяется то, что легче всего сделать неправильно: не арифметика, а
 * различение «не измерено» и «ноль». Оба на объекте означают разное, а
 * выглядеть могут одинаково.
 */
import { describe, expect, it } from "vitest";

import {
  NO_VALUE,
  formatBitrate,
  formatFps,
  isSilentStream,
} from "./streamRate";

describe("битрейт", () => {
  it("до мегабита показывается в килобитах", () => {
    expect(formatBitrate(438.1)).toBe("438 кбит/с");
    expect(formatBitrate(999)).toBe("999 кбит/с");
  });

  it("от мегабита — в мегабитах с одним знаком", () => {
    expect(formatBitrate(1000)).toBe("1.0 Мбит/с");
    expect(formatBitrate(4200)).toBe("4.2 Мбит/с");
  });

  it("«не измерен» — прочерк, и это не ноль", () => {
    // Разные состояния объекта: прочерк — «первая проба счётчика ещё не
    // с чем сравнивать», ноль — «путь заведён, байты не идут».
    expect(formatBitrate(null)).toBe(NO_VALUE);
    expect(formatBitrate(undefined)).toBe(NO_VALUE);
    expect(formatBitrate(NaN)).toBe(NO_VALUE);
  });

  it("ноль показывается нулём", () => {
    expect(formatBitrate(0)).toBe("0 кбит/с");
    expect(formatBitrate(0)).not.toBe(NO_VALUE);
  });
});

describe("FPS", () => {
  it("целое остаётся целым", () => {
    expect(formatFps(25)).toBe("25");
  });

  it("дробная часть не округляется до целого", () => {
    // 24.2 вместо 25 — это потери в сети, и «24» их прячет.
    expect(formatFps(24.2)).toBe("24.2");
  });

  it("не измерен — прочерк", () => {
    expect(formatFps(null)).toBe(NO_VALUE);
    expect(formatFps(undefined)).toBe(NO_VALUE);
  });
});

describe("молчащий поток", () => {
  it("онлайн с нулевым битрейтом — то самое состояние из §9", () => {
    expect(isSilentStream("online", 0)).toBe(true);
  });

  it("оффлайн с нулём молчащим не считается — про него уже сказал статус", () => {
    expect(isSilentStream("offline", 0)).toBe(false);
  });

  it("неизмеренный битрейт не выдаётся за молчание", () => {
    // Иначе каждый первый проход менеджера после старта воркера красил бы
    // все 120 исправных камер как аварийные.
    expect(isSilentStream("online", null)).toBe(false);
    expect(isSilentStream("online", undefined)).toBe(false);
  });

  it("идущий поток молчащим не считается", () => {
    expect(isSilentStream("online", 438)).toBe(false);
  });
});
