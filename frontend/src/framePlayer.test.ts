/**
 * Покадровый просмотр в плеере архива (SPEC §5).
 *
 * Проверяется одно свойство и все способы его сломать: **нажатие на
 * «кадр вперёд»/«кадр назад» всегда меняет кадр ровно на один**. Каждый
 * из способов ломается независимо, поэтому вынесен отдельным тестом:
 * шаг мимо кадра (промах на границе), топтание на месте (шаг меньше
 * погрешности времени презентации), прыжок на секунды (замер кадра снят
 * после перемотки) и выход за границы сегмента.
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_RATE,
  FALLBACK_FRAME_DURATION,
  MAX_FRAME_DURATION,
  MIN_FRAME_DURATION,
  PLAYBACK_RATES,
  frameDurationFrom,
  frameNumber,
  isPlaybackRate,
  positionLabel,
  stepTime,
} from "./framePlayer";

const FPS15 = 1 / 15;
const FPS30 = 1 / 30;

describe("frameDurationFrom", () => {
  it("принимает правдоподобные интервалы 15 и 30 fps", () => {
    expect(frameDurationFrom(FPS15)).toBeCloseTo(FPS15);
    expect(frameDurationFrom(FPS30)).toBeCloseTo(FPS30);
  });

  it("отбрасывает скачок после перемотки", () => {
    // Первый колбэк после seek: разница времён презентации — это длина
    // скачка. Прими её за длительность кадра — и «кадр вперёд» стал бы
    // перемоткой на четыре секунды.
    expect(frameDurationFrom(4.0)).toBeNull();
  });

  it("отбрасывает ноль и обратный ход времени", () => {
    // Два колбэка на один кадр дают 0; обратный ход бывает при смене
    // источника в том же элементе.
    expect(frameDurationFrom(0)).toBeNull();
    expect(frameDurationFrom(-0.04)).toBeNull();
  });

  it("отбрасывает нечисловые значения", () => {
    expect(frameDurationFrom(NaN)).toBeNull();
    expect(frameDurationFrom(Infinity)).toBeNull();
  });

  it("границы правдоподобия включительны по смыслу", () => {
    expect(frameDurationFrom(MIN_FRAME_DURATION)).not.toBeNull();
    expect(frameDurationFrom(MAX_FRAME_DURATION)).not.toBeNull();
    expect(frameDurationFrom(MIN_FRAME_DURATION / 2)).toBeNull();
    expect(frameDurationFrom(MAX_FRAME_DURATION * 2)).toBeNull();
  });
});

// Якорь — точная граница начала показанного кадра (mediaTime из
// requestVideoFrameCallback). Кадр N занимает [anchor, anchor + f).
describe("stepTime", () => {
  it("шаг вперёд попадает внутрь следующего кадра", () => {
    const anchor = 10;
    const to = stepTime(anchor, FPS15, 300, 1);
    expect(to).toBeGreaterThan(anchor + FPS15);
    expect(to).toBeLessThan(anchor + 2 * FPS15);
  });

  it("шаг назад попадает внутрь предыдущего кадра", () => {
    const anchor = 10;
    const to = stepTime(anchor, FPS15, 300, -1);
    expect(to).toBeGreaterThanOrEqual(anchor - FPS15);
    expect(to).toBeLessThan(anchor);
  });

  it("целится в середину кадра, а не в его границу", () => {
    // Браузер показывает кадр НЕ ПОЗЖЕ запрошенного времени. Попади шаг
    // на границу — из-за погрешности замера он через раз оставался бы на
    // том же кадре, и кнопка нажималась бы вхолостую.
    for (const anchor of [0.5, 10, 123.456]) {
      for (const dir of [1, -1] as const) {
        const to = stepTime(anchor, FPS15, 300, dir);
        const offsetInFrame = Math.abs(to - anchor) / FPS15 % 1;
        expect(offsetInFrame).toBeGreaterThan(0.25);
        expect(offsetInFrame).toBeLessThan(0.75);
      }
    }
  });

  it("ошибка замера длительности кадра не накапливается", () => {
    // Главная находка браузерной проверки: замер даёт 0.0670 там, где на
    // самом деле 0.066667. Прежняя версия строила сетку от начала файла,
    // и на десятой минуте эта разница уводила цель на целый кадр — шаг
    // вперёд срабатывал 4 раза из 10. Отсчёт от якоря обязан давать
    // одинаковое смещение и на первой секунде, и на пятой минуте.
    const measured = 0.067;
    const near = stepTime(1, measured, 3000, 1) - 1;
    const far = stepTime(280, measured, 3000, 1) - 280;
    expect(near).toBeCloseTo(far, 9);
  });

  it("не уходит за начало сегмента", () => {
    // Отрицательное currentTime браузер молча игнорирует — кнопка на
    // первом кадре выглядела бы сломанной.
    expect(stepTime(0, FPS15, 300, -1)).toBe(0);
    expect(stepTime(0.01, FPS15, 300, -1)).toBe(0);
  });

  it("не уходит за конец сегмента", () => {
    // Выход за конец переводит <video> в состояние ended.
    expect(stepTime(300, FPS15, 300, 1)).toBe(300);
    expect(stepTime(299.99, FPS15, 300, 1)).toBe(300);
  });

  it("работает, пока длительность сегмента ещё не известна", () => {
    // metadata может не успеть загрузиться к первому нажатию: duration
    // у <video> до этого момента NaN. Зажимать по нему нельзя — иначе
    // первое нажатие уводило бы плеер в 0.
    for (const unknownDuration of [NaN, 0, Infinity]) {
      expect(stepTime(5, FPS15, unknownDuration, 1)).toBeCloseTo(5 + 1.5 * FPS15, 9);
    }
  });

  it("нулевая оценка кадра подменяется запасной, а не топчется на месте", () => {
    // Ноль сюда попадает, если ни один замер не прошёл фильтр (браузер
    // без requestVideoFrameCallback). Шаг обязан остаться шагом.
    const to = stepTime(10, 0, 300, 1);
    expect(to).toBeGreaterThan(10);
    expect(to - 10).toBeLessThan(FALLBACK_FRAME_DURATION * 2);
  });

  it("шаг вперёд и назад возвращают на исходный кадр", () => {
    // В плеере якорь после шага переустанавливается на границу нового
    // кадра — здесь это воспроизводится явно.
    const anchor = 10;
    const forwardAnchor = anchor + FPS30;         // граница следующего кадра
    const back = stepTime(forwardAnchor, FPS30, 300, -1);
    expect(back).toBeGreaterThanOrEqual(anchor);
    expect(back).toBeLessThan(anchor + FPS30);
  });
});

describe("frameNumber / positionLabel", () => {
  it("считает номер кадра от начала сегмента", () => {
    expect(frameNumber(0, FPS15)).toBe(0);
    expect(frameNumber(1, FPS15)).toBe(15);
    expect(frameNumber(2, FPS30)).toBe(60);
  });

  it("в середине кадра показывает НОМЕР ЭТОГО кадра, а не следующего", () => {
    // Рабочая точка: шаг целится ровно в середину кадра, то есть подпись
    // почти всегда считается именно отсюда. Округление к ближайшему
    // показывало бы здесь номер следующего кадра — подпись разошлась бы
    // с картинкой на каждом нажатии. Поймано верификацией откатом:
    // прежние проверки брали только границы, где floor и round совпадают.
    expect(frameNumber(10 + 0.5 * FPS15, FPS15)).toBe(frameNumber(10, FPS15));
    expect(frameNumber(10 + 0.9 * FPS15, FPS15)).toBe(frameNumber(10, FPS15));
    expect(frameNumber(10 + 1.5 * FPS15, FPS15)).toBe(frameNumber(10, FPS15) + 1);
  });

  it("не отдаёт отрицательных номеров и NaN", () => {
    expect(frameNumber(-1, FPS15)).toBe(0);
    expect(positionLabel(NaN, FPS15)).toBe("0.000 с · кадр 0");
  });

  it("подпись показывает миллисекунды", () => {
    // Секунда с одним знаком не различает соседние кадры на 30 fps —
    // подпись выглядела бы застывшей при работающих кнопках.
    expect(positionLabel(12.48, FPS15)).toBe("12.480 с · кадр 187");
  });
});

describe("скорости воспроизведения", () => {
  it("набор из §5 и обычная скорость по умолчанию", () => {
    expect(PLAYBACK_RATES).toContain(0.25);
    expect(PLAYBACK_RATES).toContain(4);
    expect(DEFAULT_RATE).toBe(1);
  });

  it("отбраковывает чужое значение", () => {
    // Скорость попадает в <video>.playbackRate; отрицательная или нулевая
    // роняет плеер в браузере, а не просто игнорируется.
    expect(isPlaybackRate(2)).toBe(true);
    expect(isPlaybackRate(3)).toBe(false);
    expect(isPlaybackRate(0)).toBe(false);
    expect(isPlaybackRate(-1)).toBe(false);
    expect(isPlaybackRate("2")).toBe(false);
  });
});
