import { describe, expect, it } from "vitest";
import { describeSchedule, emptySchedule } from "./reportSchedule";

// Поля ScheduleIn на бэкенде (routers/reports.py). Схема с extra="forbid":
// лишний ключ роняет сохранение всей формы в 422, пропущенный молча
// подставляется дефолтом бэкенда, а не тем, что видел администратор.
const BACKEND_FIELDS = [
  "name", "kind", "fmt", "days", "recipients", "enabled",
  "period", "hour", "minute", "day_of_week", "day_of_month",
];

describe("пустой шаблон", () => {
  it("содержит ровно поля, которые принимает бэкенд", () => {
    expect(Object.keys(emptySchedule()).sort()).toEqual([...BACKEND_FIELDS].sort());
  });

  it("по умолчанию не включает автоотправку", () => {
    // Иначе шаблон, созданный «чтобы скачивать кнопкой», начал бы сам
    // рассылать письма — и заметили бы это получатели, а не автор.
    expect(emptySchedule().enabled).toBe(false);
  });

  it("день месяца по умолчанию в допустимом диапазоне 1..28", () => {
    const d = emptySchedule().day_of_month;
    expect(d).toBeGreaterThanOrEqual(1);
    expect(d).toBeLessThanOrEqual(28);
  });
});

describe("описание расписания словами", () => {
  it("выключённое расписание — отправка вручную", () => {
    expect(describeSchedule({ enabled: false, period: "daily" })).toBe("отправка вручную");
  });

  it("ежедневное показывает время с ведущими нулями", () => {
    expect(describeSchedule({ enabled: true, period: "daily", hour: 8, minute: 5 }))
      .toBe("ежедневно в 08:05");
  });

  it("еженедельное называет день недели", () => {
    expect(describeSchedule({ enabled: true, period: "weekly", day_of_week: 2, hour: 9, minute: 0 }))
      .toBe("по средам в 09:00");
  });

  it("ежемесячное называет число", () => {
    expect(describeSchedule({ enabled: true, period: "monthly", day_of_month: 5, hour: 7, minute: 30 }))
      .toBe("5-го числа в 07:30");
  });

  it("не падает на неполном объекте из старой БД", () => {
    expect(describeSchedule({ enabled: true, period: "weekly" })).toBe("по понедельникам в 00:00");
    expect(describeSchedule({ enabled: true })).toBe("ежедневно в 00:00");
  });
});
