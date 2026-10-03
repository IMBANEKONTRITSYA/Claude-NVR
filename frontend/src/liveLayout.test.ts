/**
 * Тесты раскладки стены и цифрового зума (SPEC §4).
 *
 * Первый набор тестов фронтенда в проекте: до этого JS-тестраннера не
 * было вовсе, хотя §21 перечисляет его в стеке, и логика интерфейса
 * проверялась только статическими проверками из `backend/tests`. Здесь
 * проверяется то, что статикой не проверить: сколько камер реально
 * попадёт на экран и куда уедет кадр при зуме.
 */
import { describe, expect, it } from "vitest";

import {
  DEFAULT_LAYOUT,
  LAYOUTS,
  MAX_ZOOM,
  applyZoom,
  clampPage,
  clampPan,
  clampZoom,
  gridColumns,
  pageCount,
  pageSlice,
  zoomTransform,
} from "./liveLayout";

const cams = (n: number) => Array.from({ length: n }, (_, i) => i + 1);

describe("раскладка мозаики", () => {
  it("предлагает ровно те размеры, что перечислены в §4", () => {
    expect([...LAYOUTS]).toEqual([1, 4, 9, 16]);
  });

  it("считает колонки по размеру мозаики", () => {
    expect(LAYOUTS.map(gridColumns)).toEqual([1, 2, 3, 4]);
  });

  it("никогда не отдаёт больше камер, чем размер мозаики", () => {
    // Главное свойство: на объекте из 250 камер (§1) на экран уходит
    // максимум 16 плиток, иначе вкладка поднимает 250 HLS-плееров.
    for (const layout of LAYOUTS) {
      for (let page = 1; page <= pageCount(250, layout); page++) {
        expect(pageSlice(cams(250), page, layout).length).toBeLessThanOrEqual(layout);
      }
    }
  });

  it("покрывает страницами весь список без пропусков и повторов", () => {
    const all = cams(250);
    for (const layout of LAYOUTS) {
      const seen: number[] = [];
      for (let page = 1; page <= pageCount(250, layout); page++) {
        seen.push(...pageSlice(all, page, layout));
      }
      expect(seen).toEqual(all);
    }
  });

  it("держит хотя бы одну страницу при пустом списке камер", () => {
    expect(pageCount(0, DEFAULT_LAYOUT)).toBe(1);
    expect(pageSlice([], 1, DEFAULT_LAYOUT)).toEqual([]);
  });

  it("приводит номер страницы при укрупнении мозаики", () => {
    // 20 камер по 4 на экран — пять страниц; после переключения на 16 их
    // остаётся две, и пятая страница обязана съехать на вторую, а не
    // оставить оператора перед пустой стеной.
    expect(clampPage(5, 20, 4)).toBe(5);
    expect(clampPage(5, 20, 16)).toBe(2);
    expect(clampPage(0, 20, 4)).toBe(1);
    expect(clampPage(NaN, 20, 4)).toBe(1);
  });

  it("отдаёт непустую страницу при съехавшем номере", () => {
    expect(pageSlice(cams(20), 99, 4)).toEqual([17, 18, 19, 20]);
  });
});

describe("цифровой зум", () => {
  it("держится в пределах 1×–8×", () => {
    expect(clampZoom(0.2)).toBe(1);
    expect(clampZoom(100)).toBe(MAX_ZOOM);
    expect(clampZoom(2.5)).toBe(2.5);
  });

  it("не даёт двигать кадр без увеличения", () => {
    expect(clampPan(0.4, 1)).toBe(0);
  });

  it("не выпускает край кадра внутрь плитки", () => {
    // При 3× кадр втрое больше плитки, значит запас с каждой стороны — 1×,
    // то есть смещение не больше единицы по модулю.
    expect(clampPan(5, 3)).toBe(1);
    expect(clampPan(-5, 3)).toBe(-1);
    expect(clampPan(0.5, 3)).toBe(0.5);
  });

  it("подтягивает смещение при уменьшении масштаба", () => {
    // Оператор отвёл кадр на краю при 8×, затем вернулся к 2× — смещение,
    // законное для 8×, при 2× открыло бы пустое поле.
    const zoomedOut = applyZoom(8, { x: 3.5, y: -3.5 }, 0.25);
    expect(zoomedOut.zoom).toBe(2);
    expect(zoomedOut.pan).toEqual({ x: 0.5, y: -0.5 });
  });

  it("не строит трансформацию без увеличения", () => {
    expect(zoomTransform(1, { x: 0, y: 0 })).toBe("none");
    expect(zoomTransform(2, { x: 0.25, y: 0 })).toBe("translate(25%, 0%) scale(2)");
  });
});
