/**
 * Ячейка «Поток аналитики» в §9-мониторинге.
 *
 * Набор причин задаёт воркер (worker/analytics_source.py); здесь
 * проверяется, что каждая из них получает подпись и цвет, и что
 * предупреждением помечены ровно ненормальные случаи. Причина без
 * ветки молча выпала бы в «ok» — то есть аварийный переезд аналитики на
 * основной поток выглядел бы штатной работой.
 */
import { describe, expect, it } from "vitest";
import { streamCell, streamLabel, streamTone } from "./analyticsStream";

describe("streamLabel", () => {
  it("называет субпоток вместе с разрешением", () => {
    expect(streamLabel({ stream: "sub", width: 704, height: 576 }))
      .toBe("Субпоток 704×576");
  });

  it("называет основной поток", () => {
    expect(streamLabel({ stream: "main", reason: "no_sub" }))
      .toBe("Основной поток");
  });

  it("обходится без разрешения, когда его нет", () => {
    expect(streamLabel({ stream: "sub", reason: "sub_resolution_unknown" }))
      .toBe("Субпоток");
  });

  it("НЕ приписывает разрешение субпотока подписи основного потока", () => {
    // Воркер отдаёт числа субпотока и после перевода на основной поток —
    // ими решение и объясняется. Но «Основной поток 352×288» — это
    // утверждение о разрешении основного потока, которого никто не мерил.
    // Числа остаются в пояснении, где сказано, чьи они.
    expect(streamLabel({ stream: "main", reason: "sub_below_floor", width: 352, height: 288 }))
      .toBe("Основной поток");
  });

  it("показывает разрешение субпотока, когда уходить было некуда", () => {
    // Здесь поток действительно субпоток, и 352×288 — его собственное
    // разрешение: подпись правдива.
    expect(streamLabel({ stream: "sub", reason: "sub_below_floor_no_main",
                         width: 352, height: 288 }))
      .toBe("Субпоток 352×288");
  });
});

describe("streamTone", () => {
  it("штатная работа по субпотоку — без предупреждения", () => {
    expect(streamTone({ stream: "sub", reason: "sub_meets_floor" })).toBe("ok");
  });

  it("камера без субпотока — без предупреждения", () => {
    expect(streamTone({ stream: "main", reason: "no_sub" })).toBe("ok");
  });

  it("переезд на основной поток — предупреждение", () => {
    expect(streamTone({ stream: "main", reason: "sub_below_floor" })).toBe("warn");
  });

  it("детекция ниже порога §15 — предупреждение", () => {
    expect(streamTone({ stream: "sub", reason: "sub_below_floor_no_main" })).toBe("warn");
  });

  it("неизмеренное разрешение — не поломка и не «всё в порядке»", () => {
    expect(streamTone({ stream: "sub", reason: "sub_resolution_unknown" })).toBe("muted");
  });

  it("незнакомая причина не притворяется предупреждением", () => {
    expect(streamTone({ stream: "sub", reason: "что-то новое" })).toBe("ok");
  });
});

describe("streamCell", () => {
  it("несёт пояснение воркера как есть", () => {
    const note = "субпоток 352×288 ниже порога 640×480 — аналитика переведена на основной поток";
    const cell = streamCell({ stream: "main", reason: "sub_below_floor",
                              width: 352, height: 288, note });
    expect(cell).toEqual({ label: "Основной поток", tone: "warn", note });
  });

  it("пустое пояснение не превращается в undefined", () => {
    expect(streamCell({ stream: "main", reason: "no_sub" }).note).toBe("");
  });
});
