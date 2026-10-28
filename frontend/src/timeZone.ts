/**
 * Часовой пояс показа и ввода времени.
 *
 * Сервер хранит и отдаёт время в UTC **без суффикса пояса**
 * (`2026-10-28T14:11:41`), и так же его принимает. До этого модуля
 * интерфейс выводил его через `new Date(ts).toLocaleString()`, а строку без
 * пояса браузер читает как **местную** — то есть показывал цифры UTC под
 * видом местного времени. На объекте в UTC+5 запись «в 15:00» значилась как
 * 10:00, фильтр «с 15:00» искал с 15:00 UTC, а Стена писала про только что
 * увиденное лицо «5 ч назад».
 *
 * Пояс берётся из настройки `display_timezone` (её начальное значение —
 * пояс системы при первом запуске, дальше её меняет администратор), а не из
 * браузера: оператор, открывший архив с ноутбука в другом поясе, должен
 * видеть то же время, что и на мониторе поста охраны.
 *
 * Модуль чистый (без DOM): переход через полночь и перевод часов проверяются
 * тестом, а не глазами по таблице.
 */
import { parseArchiveTime, formatArchiveTime } from "./archiveTimeline";

let current = "UTC";

/** Пояс из настройки; неизвестное имя не принимается — остаётся прежний. */
export function setDisplayTimeZone(tz: string | null | undefined): boolean {
  if (!tz || !isValidTimeZone(tz)) return false;
  current = tz;
  return true;
}

export function getDisplayTimeZone(): string {
  return current;
}

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function isValidTimeZone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

const partsCache = new Map<string, Intl.DateTimeFormat>();
function partsFormatter(tz: string): Intl.DateTimeFormat {
  let f = partsCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    partsCache.set(tz, f);
  }
  return f;
}

interface Wall { y: number; mo: number; d: number; h: number; mi: number; s: number }

/** Настенное время в поясе для момента UTC. */
function wallClock(ms: number, tz: string): Wall {
  const p: Record<string, number> = {};
  for (const part of partsFormatter(tz).formatToParts(new Date(ms))) {
    if (part.type !== "literal") p[part.type] = Number(part.value);
  }
  return { y: p.year, mo: p.month, d: p.day, h: p.hour === 24 ? 0 : p.hour, mi: p.minute, s: p.second };
}

/** Смещение пояса от UTC в момент `ms`, в миллисекундах (UTC+5 → +18 000 000). */
export function zoneOffsetMs(ms: number, tz: string): number {
  const w = wallClock(ms, tz);
  const asUtc = Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi, w.s);
  return asUtc - Math.floor(ms / 1000) * 1000;
}

const p2 = (n: number) => String(n).padStart(2, "0");

/** Момент → `28.10.2026, 19:11:41` в поясе показа. */
export function formatMs(ms: number, tz: string = current): string {
  if (!Number.isFinite(ms)) return "—";
  const w = wallClock(ms, tz);
  return `${p2(w.d)}.${p2(w.mo)}.${w.y}, ${p2(w.h)}:${p2(w.mi)}:${p2(w.s)}`;
}

/** Время из ответа API (UTC, с суффиксом или без) → строка в поясе показа. */
export function formatServerTime(value: string | null | undefined, tz: string = current): string {
  return formatMs(parseArchiveTime(value), tz);
}

/** Время из ответа API → миллисекунды эпохи (UTC-трактовка строки без пояса). */
export function serverTimeMs(value: string | null | undefined): number {
  return parseArchiveTime(value);
}

/**
 * Значение поля `datetime-local` (настенное время в поясе показа) → время
 * для API: UTC без суффикса, `YYYY-MM-DDTHH:MM:SS`. Пустое/битое — `""`.
 *
 * Перевод часов: смещение берётся в момент результата, а не в момент
 * «настенных цифр, прочитанных как UTC», — иначе час после перехода
 * съезжал бы на час. Несуществующее весной время (02:30 при переводе
 * 02:00→03:00) даёт ближайший существующий момент, как у любого NVR.
 */
export function localInputToUtc(value: string | null | undefined, tz: string = current): string {
  if (!value) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!m) return "";
  const wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], m[6] ? +m[6] : 0);
  let utc = wall - zoneOffsetMs(wall, tz);
  utc = wall - zoneOffsetMs(utc, tz);
  return formatArchiveTime(utc);
}

/** Время из ответа API (или момент) → значение для поля `datetime-local`. */
export function utcToLocalInput(value: string | number | null | undefined, tz: string = current): string {
  const ms = typeof value === "number" ? value : parseArchiveTime(value);
  if (!Number.isFinite(ms)) return "";
  const w = wallClock(ms, tz);
  return `${w.y}-${p2(w.mo)}-${p2(w.d)}T${p2(w.h)}:${p2(w.mi)}:${p2(w.s)}`;
}

/** Сегодняшняя дата в поясе показа, `YYYY-MM-DD`. */
export function todayInZone(tz: string = current, now: number = Date.now()): string {
  return utcToLocalInput(now, tz).slice(0, 10);
}

/** Сутки `YYYY-MM-DD` в поясе показа → окно для API (UTC без суффикса). */
export function dayWindowUtc(day: string, tz: string = current): { from: string; to: string } {
  const from = localInputToUtc(`${day}T00:00:00`, tz);
  const next = new Date(Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10) + 1));
  const nextDay = next.toISOString().slice(0, 10);
  return { from, to: localInputToUtc(`${nextDay}T00:00:00`, tz) };
}

/** Подпись пояса для интерфейса: `Asia/Yekaterinburg (UTC+05:00)`. */
export function zoneLabel(tz: string = current, now: number = Date.now()): string {
  const off = Math.round(zoneOffsetMs(now, tz) / 60000);
  const sign = off < 0 ? "−" : "+";
  const a = Math.abs(off);
  return `${tz} (UTC${sign}${p2(Math.floor(a / 60))}:${p2(a % 60)})`;
}
