/**
 * Расписание отчётов (SPEC §8) — чистая часть, пригодная для теста.
 *
 * Вынесено из Reports.tsx по той же причине, что и settingsPayload:
 * значения по умолчанию должны совпадать с моделью бэкенда
 * (`models.ReportSchedule`), а расхождение здесь молчаливое — форма
 * отправит поле, которого схема не ждёт (422 на всю форму), либо не
 * отправит обязательное.
 */

const WEEKDAYS = [
  "понедельникам", "вторникам", "средам", "четвергам",
  "пятницам", "субботам", "воскресеньям",
];

/** Пустой шаблон. Значения — те же, что в models.ReportSchedule. */
export function emptySchedule() {
  return {
    name: "",
    kind: "appearances",
    fmt: "xlsx",
    days: 7,
    recipients: "",
    enabled: false,
    period: "daily",
    hour: 8,
    minute: 0,
    day_of_week: 0,
    day_of_month: 1,
  };
}

/**
 * Человеческое описание расписания для карточки шаблона.
 *
 * Нужно именно словами: набор полей `period/hour/day_of_week` читается
 * администратором как «а когда оно всё-таки придёт?», и ошибка в
 * настройке (выбран день недели, но период «ежемесячно») видна только
 * если написать результат прописью.
 */
export function describeSchedule(s: {
  enabled?: boolean; period?: string; hour?: number; minute?: number;
  day_of_week?: number; day_of_month?: number;
}): string {
  if (!s.enabled) return "отправка вручную";
  const time = `${String(s.hour ?? 0).padStart(2, "0")}:${String(s.minute ?? 0).padStart(2, "0")}`;
  if (s.period === "weekly") {
    return `по ${WEEKDAYS[s.day_of_week ?? 0] ?? "понедельникам"} в ${time}`;
  }
  if (s.period === "monthly") {
    return `${s.day_of_month ?? 1}-го числа в ${time}`;
  }
  return `ежедневно в ${time}`;
}
