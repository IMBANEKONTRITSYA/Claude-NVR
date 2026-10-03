import { describe, expect, it, vi } from "vitest";
import {
  clockLabel,
  coverageBars,
  dayWindow,
  fractionToTime,
  locateAt,
  nextIndex,
  parseArchiveTime,
  positionToClock,
  recordedLabel,
  timeToFraction,
  TimelineSegment,
} from "./archiveTimeline";

/** Три пятиминутных сегмента подряд и один после двадцатиминутного перерыва. */
const SEGMENTS: TimelineSegment[] = [
  { id: 1, started_at: "2026-03-01T03:00:00", ended_at: "2026-03-01T03:05:00", duration_sec: 300 },
  { id: 2, started_at: "2026-03-01T03:05:00", ended_at: "2026-03-01T03:10:00", duration_sec: 300 },
  { id: 3, started_at: "2026-03-01T03:10:00", ended_at: "2026-03-01T03:15:00", duration_sec: 300 },
  { id: 4, started_at: "2026-03-01T03:35:00", ended_at: "2026-03-01T03:40:00", duration_sec: 300 },
];

const at = (s: string) => parseArchiveTime(`2026-03-01T${s}`);

describe("parseArchiveTime", () => {
  it("трактует наивную строку как UTC, а не как местное время", () => {
    // Ровно та ошибка, ради которой разбор написан руками: Date.parse
    // без суффикса зоны отдал бы местное время, и на сервере в UTC+3 вся
    // шкала разъехалась бы с записями на три часа.
    expect(parseArchiveTime("2026-03-01T03:00:00")).toBe(Date.UTC(2026, 2, 1, 3, 0, 0));
  });

  it("принимает пробел вместо T и дробные секунды", () => {
    expect(parseArchiveTime("2026-03-01 03:00:00")).toBe(Date.UTC(2026, 2, 1, 3, 0, 0));
    expect(parseArchiveTime("2026-03-01T03:00:00.250")).toBe(Date.UTC(2026, 2, 1, 3, 0, 0, 250));
  });

  it("размеченное зоной значение разбирает движок", () => {
    expect(parseArchiveTime("2026-03-01T06:00:00+03:00")).toBe(Date.UTC(2026, 2, 1, 3, 0, 0));
  });

  it("не зависит от пояса машины, на которой идёт прогон", () => {
    // Без этого блока проверка выше ложно-зелёная: раннер CI живёт в UTC,
    // где `Date.parse` наивной строки совпадает с `Date.UTC`, и подмена
    // разбора на движковый прошла бы незамеченной. Целевой сервер стоит
    // не в UTC, и там расхождение — это часы, на которые шкала разъедется
    // с записями. Пояс подменяется на время блока: Node перечитывает
    // `TZ` при каждой операции с датой.
    vi.stubEnv("TZ", "Europe/Moscow");
    try {
      expect(parseArchiveTime("2026-03-01T03:00:00")).toBe(Date.UTC(2026, 2, 1, 3, 0, 0));
      // Контроль на то, что подмена пояса вообще подействовала, — иначе
      // блок был бы декорацией.
      expect(Date.parse("2026-03-01T03:00:00")).not.toBe(Date.UTC(2026, 2, 1, 3, 0, 0));
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("мусор и пустое — NaN, а не ноль эпохи", () => {
    // Ноль эпохи прошёл бы дальше как «1970 год» и увёл бы шкалу в начало
    // времён вместо честного «времени нет».
    expect(parseArchiveTime("")).toBeNaN();
    expect(parseArchiveTime(null)).toBeNaN();
    expect(parseArchiveTime("вчера")).toBeNaN();
  });
});

describe("locateAt", () => {
  it("находит сегмент и смещение внутри него", () => {
    expect(locateAt(SEGMENTS, at("03:07:30"))).toEqual({ index: 1, offsetSec: 150 });
  });

  it("граница сегмента отдаётся следующему, а не предыдущему", () => {
    // Конец сегмента исключён: иначе клик ровно в 03:05 попал бы в самый
    // конец первого файла и воспроизведение сразу же перескочило дальше.
    expect(locateAt(SEGMENTS, at("03:05:00"))).toEqual({ index: 1, offsetSec: 0 });
  });

  it("клик в дыру перематывает к следующей записи, а не гасит плеер", () => {
    // Поведение промышленного NVR: в 03:20 записи нет, ближайшая — 03:35.
    expect(locateAt(SEGMENTS, at("03:20:00"))).toEqual({ index: 3, offsetSec: 0 });
  });

  it("время до начала окна ведёт на первый сегмент", () => {
    expect(locateAt(SEGMENTS, at("02:00:00"))).toEqual({ index: 0, offsetSec: 0 });
  });

  it("после последней записи — null, идти некуда", () => {
    expect(locateAt(SEGMENTS, at("23:00:00"))).toBeNull();
  });

  it("пустая цепочка и нечисло — null, а не исключение", () => {
    expect(locateAt([], at("03:00:00"))).toBeNull();
    expect(locateAt(SEGMENTS, NaN)).toBeNull();
  });
});

describe("positionToClock", () => {
  it("переводит позицию внутри файла в настенное время", () => {
    expect(positionToClock(SEGMENTS, 1, 150)).toBe(at("03:07:30"));
  });

  it("несуществующий индекс — NaN, а не время начала архива", () => {
    expect(positionToClock(SEGMENTS, 99, 0)).toBeNaN();
  });

  it("отрицательное смещение не уводит время назад", () => {
    expect(positionToClock(SEGMENTS, 0, -10)).toBe(at("03:00:00"));
  });
});

describe("nextIndex", () => {
  it("даёт следующее звено цепочки", () => {
    expect(nextIndex(SEGMENTS, 0)).toBe(1);
  });

  it("перерыв не обрывает цепочку — просмотр перескакивает его", () => {
    // За сегментом, кончающимся в 03:15, идёт начавшийся в 03:35.
    expect(nextIndex(SEGMENTS, 2)).toBe(3);
  });

  it("за последним звеном — null", () => {
    expect(nextIndex(SEGMENTS, 3)).toBeNull();
  });
});

describe("coverageBars", () => {
  const from = at("00:00:00");
  const to = from + 24 * 3600 * 1000;

  it("раскладывает диапазон в проценты ширины суток", () => {
    const bars = coverageBars(
      [{ start: "2026-03-01T06:00:00", end: "2026-03-01T12:00:00" }], from, to,
    );
    expect(bars).toHaveLength(1);
    expect(bars[0].leftPct).toBeCloseTo(25, 6);
    expect(bars[0].widthPct).toBeCloseTo(25, 6);
  });

  it("обрезает диапазон, вылезающий за окно", () => {
    const bars = coverageBars(
      [{ start: "2026-02-28T22:00:00", end: "2026-03-01T06:00:00" }], from, to,
    );
    expect(bars[0].leftPct).toBe(0);
    expect(bars[0].widthPct).toBeCloseTo(25, 6);
  });

  it("диапазон вне окна не даёт полоски", () => {
    expect(coverageBars(
      [{ start: "2026-03-05T00:00:00", end: "2026-03-05T01:00:00" }], from, to,
    )).toEqual([]);
  });

  it("секунда записи в сутках остаётся видимой", () => {
    // 1 с / 24 ч = 0.001 % ширины — полоска, которой не видно, читается
    // как «записи нет», а это неправда.
    const bars = coverageBars(
      [{ start: "2026-03-01T06:00:00", end: "2026-03-01T06:00:01" }], from, to,
    );
    expect(bars[0].widthPct).toBeGreaterThanOrEqual(0.15);
  });

  it("расширенная до минимума полоска у правого края не вылезает за шкалу", () => {
    const bars = coverageBars(
      [{ start: "2026-03-01T23:59:59", end: "2026-03-02T00:00:00" }], from, to,
    );
    expect(bars[0].leftPct + bars[0].widthPct).toBeLessThanOrEqual(100);
  });

  it("вырожденное окно не делит на ноль", () => {
    expect(coverageBars([{ start: "2026-03-01T06:00:00", end: "2026-03-01T07:00:00" }],
                        from, from)).toEqual([]);
  });
});

describe("шкала: клик и обратный перевод", () => {
  const { fromMs, toMs } = dayWindow("2026-03-01");

  it("сутки считаются от полуночи UTC архива", () => {
    expect(fromMs).toBe(Date.UTC(2026, 2, 1, 0, 0, 0));
    expect(toMs - fromMs).toBe(24 * 3600 * 1000);
  });

  it("середина полосы — полдень", () => {
    expect(fractionToTime(0.5, fromMs, toMs)).toBe(at("12:00:00"));
  });

  it("клик за пределами полосы зажимается её краями", () => {
    expect(fractionToTime(-1, fromMs, toMs)).toBe(fromMs);
    expect(fractionToTime(2, fromMs, toMs)).toBe(toMs);
  });

  it("перевод туда и обратно совпадает", () => {
    expect(timeToFraction(fractionToTime(0.25, fromMs, toMs), fromMs, toMs)).toBeCloseTo(0.25, 9);
  });
});

describe("подписи", () => {
  it("время показывается в шкале архива, а не в поясе браузера", () => {
    expect(clockLabel(at("03:40:07"))).toBe("01.03.2026, 03:40:07");
  });

  it("нечисло даёт прочерк, а не Invalid Date", () => {
    expect(clockLabel(NaN)).toBe("—");
  });

  it("подпись не съезжает вместе с поясом машины", () => {
    // Пара к проверке разбора: подпись и шкала обязаны быть в одной
    // шкале времени. `toLocaleString` вместо getUTC* показал бы 06:40:07
    // там, где в записи 03:40:07.
    vi.stubEnv("TZ", "Europe/Moscow");
    try {
      expect(clockLabel(at("03:40:07"))).toBe("01.03.2026, 03:40:07");
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("сколько записано за окно", () => {
    expect(recordedLabel(19 * 3600 + 43 * 60)).toBe("19 ч 43 мин");
    expect(recordedLabel(120)).toBe("2 мин");
    expect(recordedLabel(7)).toBe("7 с");
    expect(recordedLabel(0)).toBe("нет записи");
  });
});
