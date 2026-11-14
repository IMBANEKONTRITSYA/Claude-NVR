import { describe, expect, it } from "vitest";
import {
  ALL_DAYS, EMPTY_SCHEDULE, SCHEDULE_PRESETS, WORKDAYS,
  crossesMidnight, describeWindow, schedulePayload, scheduleFromCamera,
} from "./detectionSchedule";

describe("schedulePayload", () => {
  it("выключенное расписание уходит как null", () => {
    // null — «детекция круглосуточно». Пустой объект означал бы то же
    // самое, но не говорил бы, что расписания не задавали.
    expect(schedulePayload({ enabled: false, windows: [{ days: ALL_DAYS, start: "08:00", end: "18:00" }] }))
      .toBeNull();
  });

  it("включённое расписание без окон уходит как null, а не как «никогда»", () => {
    // На сервере пустой список окон означает «не детектировать никогда».
    // Отправить его случайно — включив галочку и не успев добавить окно —
    // это незаметно выключить аналитику на камере.
    expect(schedulePayload({ enabled: true, windows: [] })).toBeNull();
  });

  it("отсутствующее расписание уходит как null", () => {
    expect(schedulePayload(null)).toBeNull();
    expect(schedulePayload(undefined)).toBeNull();
    expect(schedulePayload(EMPTY_SCHEDULE)).toBeNull();
  });

  it("заполненное расписание уходит целиком", () => {
    const s = { enabled: true, windows: [{ days: WORKDAYS, start: "08:00", end: "18:00" }] };
    expect(schedulePayload(s)).toEqual(s);
  });
});

describe("describeWindow", () => {
  it("окно через полночь помечено явно", () => {
    // Без подписи администратор не отличит законную «ночную охрану» от
    // опечатки и «исправит» 22:00–06:00 на 06:00–22:00 — то есть ровно на
    // противоположное окно.
    expect(describeWindow({ days: ALL_DAYS, start: "22:00", end: "06:00" }))
      .toContain("через полночь");
  });

  it("обычное окно не помечено как ночное", () => {
    expect(describeWindow({ days: ALL_DAYS, start: "08:00", end: "18:00" }))
      .not.toContain("через полночь");
  });

  it("дни сворачиваются в привычные названия", () => {
    expect(describeWindow({ days: WORKDAYS, start: "08:00", end: "18:00" })).toContain("Пн–Пт");
    expect(describeWindow({ days: ALL_DAYS, start: "08:00", end: "18:00" })).toContain("Ежедневно");
    expect(describeWindow({ days: [5, 6], start: "08:00", end: "18:00" })).toContain("Сб–Вс");
    expect(describeWindow({ days: [0, 2], start: "08:00", end: "18:00" })).toContain("Пн, Ср");
  });

  it("окно без дней названо «никогда», а не пустой строкой", () => {
    // Снять все дни — законное состояние формы на полпути; подпись должна
    // объяснять, почему окно ничего не делает.
    expect(describeWindow({ days: [], start: "08:00", end: "18:00" })).toContain("никогда");
  });

  it("равные границы описаны как круглосуточно", () => {
    expect(describeWindow({ days: ALL_DAYS, start: "00:00", end: "00:00" })).toContain("круглосуточно");
  });
});

describe("crossesMidnight", () => {
  it("сравнивает строки времени, а не только часы", () => {
    expect(crossesMidnight({ days: ALL_DAYS, start: "08:30", end: "08:00" })).toBe(true);
    expect(crossesMidnight({ days: ALL_DAYS, start: "08:00", end: "08:30" })).toBe(false);
  });
});

describe("scheduleFromCamera", () => {
  it("камера без расписания даёт пустую форму", () => {
    expect(scheduleFromCamera(null)).toEqual(EMPTY_SCHEDULE);
    expect(scheduleFromCamera({})).toEqual(EMPTY_SCHEDULE);
    expect(scheduleFromCamera("мусор")).toEqual(EMPTY_SCHEDULE);
  });

  it("расписание камеры открывается в форме как есть", () => {
    // Форма обязана вернуть расписание обратно без изменений: иначе
    // сохранение любой другой правки камеры стирало бы его.
    const raw = { enabled: true, windows: [{ days: [0, 1], start: "22:00", end: "06:00" }] };
    expect(scheduleFromCamera(raw)).toEqual(raw);
  });

  it("битые дни отбрасываются, окно остаётся", () => {
    const got = scheduleFromCamera({ enabled: true, windows: [{ days: [0, 9, "пн"], start: "08:00", end: "18:00" }] });
    expect(got.windows[0].days).toEqual([0]);
  });
});

describe("готовые графики", () => {
  it("каждый пресет — валидное непустое расписание", () => {
    for (const preset of SCHEDULE_PRESETS) {
      expect(preset.windows.length).toBeGreaterThan(0);
      expect(schedulePayload({ enabled: true, windows: preset.windows })).not.toBeNull();
    }
  });

  it("ночной пресет действительно ночной", () => {
    // Страховка от правки, которая переставит границы местами: пресет
    // «Ночь» обязан переходить через полночь, иначе он станет «днём».
    const night = SCHEDULE_PRESETS.find(p => p.label.startsWith("Ночь"))!;
    expect(crossesMidnight(night.windows[0])).toBe(true);
  });
});
