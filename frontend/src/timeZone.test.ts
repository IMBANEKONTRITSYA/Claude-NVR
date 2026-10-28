import { describe, expect, it, afterEach } from "vitest";
import {
  dayWindowUtc, formatServerTime, getDisplayTimeZone, localInputToUtc,
  setDisplayTimeZone, todayInZone, utcToLocalInput, zoneLabel, zoneOffsetMs,
} from "./timeZone";

afterEach(() => { setDisplayTimeZone("UTC"); });

describe("показ времени сервера", () => {
  it("UTC без суффикса показывается в поясе объекта, а не как местное время браузера", () => {
    // Журнал аудита на объекте: запрос в 14:11:41 UTC. В UTC+5 это 19:11.
    expect(formatServerTime("2026-10-28T14:11:41.483946", "Asia/Yekaterinburg"))
      .toBe("28.10.2026, 19:11:41");
  });

  it("строка с суффиксом зоны тоже понимается", () => {
    expect(formatServerTime("2026-10-28T14:11:41Z", "Europe/Moscow")).toBe("28.10.2026, 17:11:41");
  });

  it("переход через полночь — меняется и дата", () => {
    expect(formatServerTime("2026-10-28T21:30:00", "Asia/Yekaterinburg")).toBe("29.10.2026, 02:30:00");
  });

  it("пустое и битое — прочерк, а не Invalid Date", () => {
    expect(formatServerTime(null)).toBe("—");
    expect(formatServerTime("вчера")).toBe("—");
  });
});

describe("ввод фильтров", () => {
  it("«с 15:00» в UTC+5 — это 10:00 UTC для API", () => {
    expect(localInputToUtc("2026-10-28T15:00", "Asia/Yekaterinburg")).toBe("2026-10-28T10:00:00");
  });

  it("обратный перевод для поля ввода — то же настенное время", () => {
    expect(utcToLocalInput("2026-10-28T10:00:00", "Asia/Yekaterinburg")).toBe("2026-10-28T15:00:00");
  });

  it("туда и обратно без потерь в поясе с переводом часов", () => {
    for (const local of ["2026-03-29T01:30:00", "2026-03-29T04:30:00", "2026-10-25T01:30:00", "2026-07-01T12:00:00"]) {
      expect(utcToLocalInput(localInputToUtc(local, "Europe/Berlin"), "Europe/Berlin")).toBe(local);
    }
  });

  it("летнее время учитывается по моменту, а не по зимнему смещению", () => {
    // Берлин: 12:00 летом — 10:00 UTC (UTC+2), зимой — 11:00 UTC (UTC+1).
    expect(localInputToUtc("2026-07-01T12:00", "Europe/Berlin")).toBe("2026-07-01T10:00:00");
    expect(localInputToUtc("2026-01-15T12:00", "Europe/Berlin")).toBe("2026-01-15T11:00:00");
  });

  it("пустое поле — пустой параметр, а не полночь 1970 года", () => {
    expect(localInputToUtc("", "Europe/Moscow")).toBe("");
    expect(localInputToUtc("28.10.2026", "Europe/Moscow")).toBe("");
  });
});

describe("сутки на шкале архива", () => {
  it("сутки 28.10 в UTC+5 — с 19:00 UTC 27.10 до 19:00 UTC 28.10", () => {
    expect(dayWindowUtc("2026-10-28", "Asia/Yekaterinburg"))
      .toEqual({ from: "2026-10-27T19:00:00", to: "2026-10-28T19:00:00" });
  });

  it("«сегодня» — по поясу объекта: в 21:00 UTC в UTC+5 уже следующий день", () => {
    const now = Date.UTC(2026, 9, 28, 21, 0, 0);
    expect(todayInZone("Asia/Yekaterinburg", now)).toBe("2026-10-29");
    expect(todayInZone("UTC", now)).toBe("2026-10-28");
  });

  it("сутки перевода часов — 25 часов, а не 24", () => {
    const w = dayWindowUtc("2026-10-25", "Europe/Berlin");
    const hours = (Date.parse(w.to + "Z") - Date.parse(w.from + "Z")) / 3_600_000;
    expect(hours).toBe(25);
  });
});

describe("настройка пояса", () => {
  it("неизвестное имя не принимается и не ломает показ", () => {
    setDisplayTimeZone("Asia/Yekaterinburg");
    expect(setDisplayTimeZone("Mars/Olympus")).toBe(false);
    expect(getDisplayTimeZone()).toBe("Asia/Yekaterinburg");
  });

  it("подпись пояса со смещением", () => {
    expect(zoneLabel("Asia/Yekaterinburg", Date.UTC(2026, 9, 28))).toBe("Asia/Yekaterinburg (UTC+05:00)");
    expect(zoneLabel("America/New_York", Date.UTC(2026, 0, 15))).toBe("America/New_York (UTC−05:00)");
    expect(zoneOffsetMs(Date.UTC(2026, 9, 28), "UTC")).toBe(0);
  });
});
