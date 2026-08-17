/**
 * Расписание детекции камеры (SPEC §6) — форма и её подписи.
 *
 * Логика вынесена из компонента по той же причине, что и в других формах
 * проекта: проверять её в разметке нечем, а ошибиться есть где. Здесь это
 * две вещи.
 *
 * Первая — **окно через полночь**. `22:00–06:00` это законная «ночная
 * охрана», а не опечатка, и интерфейс обязан показывать, что окно
 * переходит на следующие сутки. Без подписи администратор, задавший ночное
 * окно, не отличит его от неверно введённого и, скорее всего, «исправит»
 * на 06:00–22:00 — то есть ровно на противоположное.
 *
 * Вторая — **что уходит на сервер**. Снятая галочка «включить расписание»
 * должна давать `null` (детекция круглосуточно), а не пустой объект: у
 * колонки эти значения означают одно и то же, но `null` честно говорит,
 * что расписания не задавали.
 */

export type DetectionWindow = { days: number[]; start: string; end: string };
export type DetectionSchedule = { enabled: boolean; windows: DetectionWindow[] };

/** 0 = понедельник — как datetime.weekday() и как расписание отчётов (§8). */
export const DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

export const WORKDAYS = [0, 1, 2, 3, 4];
export const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];

/** Совпадает с потолком воркера (MAX_WINDOWS) и схемы бэкенда. */
export const MAX_WINDOWS = 10;

export const DEFAULT_WINDOW: DetectionWindow = { days: ALL_DAYS, start: "08:00", end: "18:00" };

export const EMPTY_SCHEDULE: DetectionSchedule = { enabled: false, windows: [] };

/** Готовые графики: типовой объект настраивается одним нажатием, а не
 *  вводом четырёх полей, где легче всего перепутать ночное окно. */
export const SCHEDULE_PRESETS: { label: string; windows: DetectionWindow[] }[] = [
  { label: "Рабочие часы (Пн–Пт 08:00–18:00)",
    windows: [{ days: WORKDAYS, start: "08:00", end: "18:00" }] },
  { label: "Ночь (22:00–06:00)",
    windows: [{ days: ALL_DAYS, start: "22:00", end: "06:00" }] },
  { label: "Нерабочее время (ночь + выходные)",
    windows: [
      { days: ALL_DAYS, start: "18:00", end: "08:00" },
      { days: [5, 6], start: "00:00", end: "00:00" },
    ] },
];

/** Переходит ли окно через полночь. */
export function crossesMidnight(w: DetectionWindow): boolean {
  return w.start > w.end;
}

function describeDays(days: number[]): string {
  const uniq = [...new Set(days)].sort((a, b) => a - b);
  if (uniq.length === 0) return "никогда";
  if (uniq.length === 7) return "Ежедневно";
  if (uniq.join() === WORKDAYS.join()) return "Пн–Пт";
  if (uniq.join() === "5,6") return "Сб–Вс";
  return uniq.map(d => DAY_LABELS[d] ?? "?").join(", ");
}

/** Подпись окна для формы. */
export function describeWindow(w: DetectionWindow): string {
  const days = describeDays(w.days);
  if (w.start === w.end) return `${days}, круглосуточно`;
  const tail = crossesMidnight(w) ? " (через полночь, до утра следующего дня)" : "";
  return `${days}, ${w.start}–${w.end}${tail}`;
}

/**
 * Значение поля `detection_schedule` для API.
 *
 * `null` — «детекция круглосуточно»: и когда галочка снята, и когда
 * расписание включено, но ни одного окна не задано. Второе важнее: пустой
 * список окон означает на сервере «не детектировать никогда», и отправить
 * его случайно, просто включив галочку и не успев добавить окно, — прямой
 * способ незаметно выключить аналитику на камере.
 */
export function schedulePayload(s: DetectionSchedule | null | undefined): DetectionSchedule | null {
  if (!s?.enabled || !s.windows?.length) return null;
  return { enabled: true, windows: s.windows.slice(0, MAX_WINDOWS) };
}

/** Расписание камеры из ответа API — в состояние формы. */
export function scheduleFromCamera(raw: any): DetectionSchedule {
  if (!raw || typeof raw !== "object" || !Array.isArray(raw.windows)) return EMPTY_SCHEDULE;
  return {
    enabled: !!raw.enabled,
    windows: raw.windows.map((w: any) => ({
      days: Array.isArray(w?.days) ? w.days.filter((d: any) => Number.isInteger(d) && d >= 0 && d <= 6) : ALL_DAYS,
      start: typeof w?.start === "string" ? w.start : "00:00",
      end: typeof w?.end === "string" ? w.end : "00:00",
    })),
  };
}
