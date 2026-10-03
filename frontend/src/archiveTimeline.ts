/**
 * Шкала архива и непрерывное воспроизведение через границы файлов (SPEC §5).
 *
 * §5 требует непрерывной записи и встроенного плеера. Запись непрерывная,
 * а плеер до сих пор играл **один файл**: оператор выбирал строку в
 * таблице сегментов, смотрел свои пять минут и упирался в конец. Событие,
 * растянутое на два сегмента, приходилось досматривать вторым кликом, а
 * «что было в 03:40» искалось перебором 288 строк за сутки.
 *
 * Здесь — арифметика, которая превращает список сегментов в шкалу времени:
 * перевод настенного времени в пару «файл + смещение», обратный перевод
 * для подписи, и раскладка диапазонов покрытия в проценты ширины полосы.
 *
 * **Время архива наивное и трактуется как UTC.** Так его хранит
 * `video_segments` (TIMESTAMP WITHOUT TIME ZONE), так его принимает
 * экспорт фрагмента, и так же его читает страница «Архив», не пропуская
 * через `new Date()` без явного разбора. Разбор здесь ручной, а не
 * `Date.parse`: строка без суффикса зоны трактуется движком как **местное**
 * время, и на сервере в UTC+3 шкала разъехалась бы с самими записями ровно
 * на три часа — причём беззвучно.
 *
 * Модуль чистый (без DOM) по той же причине, что `framePlayer` и
 * `liveLayout`: переход через границу сегмента и попадание в дыру
 * проверяются тестом, а не глазами по видео.
 */

/** Сегмент цепочки воспроизведения — то, что отдаёт `/api/archive/timeline`. */
export interface TimelineSegment {
  id: number;
  started_at: string;
  ended_at: string;
  duration_sec: number;
}

/** Непрерывный кусок записи: между `start` и `end` дыр нет. */
export interface TimelineRange {
  start: string;
  end: string;
}

/** Позиция воспроизведения: индекс сегмента в цепочке и смещение внутри него. */
export interface Position {
  index: number;
  offsetSec: number;
}

/** Полоска покрытия в процентах ширины шкалы. */
export interface Bar {
  leftPct: number;
  widthPct: number;
}

/**
 * Наивная строка времени архива → миллисекунды эпохи, как UTC.
 *
 * Принимает и `2026-03-01T10:00:00`, и вариант с пробелом вместо `T`, и
 * дробные секунды, и уже размеченное зоной значение (тогда разбор отдаётся
 * движку — зона в строке есть, гадать не о чем). `NaN` — «не время»,
 * вызывающая сторона обязана это проверить.
 */
export function parseArchiveTime(value: string | null | undefined): number {
  if (!value) return NaN;
  const s = String(value).trim();
  // Явная зона (Z или ±HH:MM в хвосте) — строку разбирает движок.
  if (/(Z|[+-]\d{2}:?\d{2})$/.test(s)) return Date.parse(s);
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?$/.exec(s);
  if (!m) return NaN;
  const sec = m[6] ? Number(m[6]) : 0;
  return Date.UTC(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4]), Number(m[5]), Math.floor(sec),
    Math.round((sec % 1) * 1000),
  );
}

/** Обратно: миллисекунды → `YYYY-MM-DDTHH:MM:SS` в той же шкале архива. */
export function formatArchiveTime(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  return new Date(ms).toISOString().slice(0, 19);
}

/** Подпись позиции на шкале: `01.03.2026, 03:40:07`. */
export function clockLabel(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}, ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

/**
 * Настенное время → сегмент и смещение внутри него.
 *
 * Сегменты обязаны идти по возрастанию начала — так их отдаёт эндпоинт.
 *
 * Попадание в **дыру** (записи в этот момент нет) не является ошибкой и не
 * даёт `null`: возвращается начало ближайшего следующего сегмента. Это
 * поведение промышленного NVR — клик по пустому месту шкалы перематывает
 * к тому, что записано дальше, а не гасит плеер. `null` — только когда
 * дальше нет ничего.
 */
export function locateAt(segments: TimelineSegment[], ms: number): Position | null {
  if (!segments.length || !Number.isFinite(ms)) return null;
  for (let i = 0; i < segments.length; i++) {
    const start = parseArchiveTime(segments[i].started_at);
    const end = parseArchiveTime(segments[i].ended_at);
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    if (ms < start) return { index: i, offsetSec: 0 };
    if (ms < end) return { index: i, offsetSec: (ms - start) / 1000 };
  }
  return null;
}

/**
 * Обратный перевод: позиция внутри сегмента → настенное время.
 *
 * Нужна для подписи «сейчас на экране 03:40:07» и для того, чтобы граница
 * экспортируемого фрагмента бралась с того места, которое оператор
 * действительно смотрит.
 */
export function positionToClock(
  segments: TimelineSegment[], index: number, offsetSec: number,
): number {
  const seg = segments[index];
  if (!seg) return NaN;
  const start = parseArchiveTime(seg.started_at);
  if (!Number.isFinite(start)) return NaN;
  const off = Number.isFinite(offsetSec) && offsetSec > 0 ? offsetSec : 0;
  return start + off * 1000;
}

/**
 * Следующее звено цепочки — то, чем закрывается разрыв на границе файлов.
 *
 * `null` означает конец окна: дальше плееру идти некуда. Дыра **не**
 * прерывает цепочку — за концом сегмента следует ближайший записанный, и
 * просмотр перескакивает перерыв, вместо того чтобы останавливаться на
 * нём. Перерыв при этом виден на шкале, то есть от оператора не спрятан.
 */
export function nextIndex(segments: TimelineSegment[], index: number): number | null {
  const next = index + 1;
  return next >= 0 && next < segments.length ? next : null;
}

/**
 * Диапазоны покрытия → полоски шкалы в процентах.
 *
 * Диапазоны за пределами окна отбрасываются, пересекающие границу —
 * обрезаются. Нулевая ширина заменяется минимальной видимой: секунда
 * записи в сутках — это 0.001 % ширины, то есть полоска, которой не
 * видно; отсутствие полоски читается как «записи нет», а это неправда.
 */
export function coverageBars(
  ranges: TimelineRange[], windowStartMs: number, windowEndMs: number,
  minWidthPct = 0.15,
): Bar[] {
  const span = windowEndMs - windowStartMs;
  if (!(span > 0)) return [];
  const out: Bar[] = [];
  for (const r of ranges) {
    const rawStart = parseArchiveTime(r.start);
    const rawEnd = parseArchiveTime(r.end);
    if (!Number.isFinite(rawStart) || !Number.isFinite(rawEnd)) continue;
    const start = Math.max(rawStart, windowStartMs);
    const end = Math.min(rawEnd, windowEndMs);
    if (end <= start) continue;
    const leftPct = ((start - windowStartMs) / span) * 100;
    const widthPct = Math.max(((end - start) / span) * 100, minWidthPct);
    // Полоска, расширенная до минимума у самого края, не должна вылезать
    // за шкалу — она сдвигается внутрь, а не растёт наружу.
    out.push({ leftPct: Math.min(leftPct, 100 - widthPct), widthPct });
  }
  return out;
}

/** Клик по шкале: доля ширины [0..1] → настенное время окна. */
export function fractionToTime(
  fraction: number, windowStartMs: number, windowEndMs: number,
): number {
  const f = Math.min(Math.max(Number.isFinite(fraction) ? fraction : 0, 0), 1);
  return windowStartMs + f * (windowEndMs - windowStartMs);
}

/** Обратное: время → доля ширины, для положения курсора шкалы. */
export function timeToFraction(
  ms: number, windowStartMs: number, windowEndMs: number,
): number {
  const span = windowEndMs - windowStartMs;
  if (!(span > 0) || !Number.isFinite(ms)) return 0;
  return Math.min(Math.max((ms - windowStartMs) / span, 0), 1);
}

/** Окно суток по дате `YYYY-MM-DD` (шкала архива — сутки, как у NVR). */
export function dayWindow(day: string): { fromMs: number; toMs: number } {
  const fromMs = parseArchiveTime(`${day}T00:00:00`);
  return { fromMs, toMs: fromMs + 24 * 3600 * 1000 };
}

/** «19 ч 43 мин» — сколько записи в окне; для подписи под шкалой. */
export function recordedLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "нет записи";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h === 0 && m === 0) return `${total} с`;
  if (h === 0) return `${m} мин`;
  return `${h} ч ${m} мин`;
}
