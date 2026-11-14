/**
 * Удержание кнопки на пульте PTZ (SPEC §4).
 *
 * Проверяется одно свойство и его края: **удержание всегда заканчивается
 * командой «стоп»**. Не отправленный «стоп» оставляет купол в движении, и
 * заметно это не в браузере, а на объекте — камера уезжает с точки. Все
 * способы «не отправить» перечислены отдельными тестами, потому что каждый
 * из них ломается независимо: отпускание за пределами кнопки,
 * размонтирование компонента, ошибка сети в середине удержания.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PTZ_REPEAT_MS, PtzHold } from "./ptz";

function makeHold(overrides: Partial<{ move: any; stop: any }> = {}) {
  const moves: any[] = [];
  const stops: number[] = [];
  const hold = new PtzHold({
    move: overrides.move ?? ((v: any) => { moves.push(v); return Promise.resolve(); }),
    stop: overrides.stop ?? (() => { stops.push(1); return Promise.resolve(); }),
  });
  return { hold, moves, stops };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("PtzHold", () => {
  it("шлёт команду сразу по нажатию, не дожидаясь первого тика", () => {
    const { hold, moves } = makeHold();
    hold.start({ pan: 0.5 });
    // Задержка на старте была бы заметна как «пульт тормозит»: оператор
    // короткими нажатиями доводит камеру до места.
    expect(moves).toEqual([{ pan: 0.5 }]);
  });

  it("повторяет команду, пока кнопка нажата", async () => {
    const { hold, moves } = makeHold();
    hold.start({ pan: 0.5 });
    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(PTZ_REPEAT_MS);
    }
    // Повтор продлевает движение: сама ONVIF-команда живёт ограниченное
    // время (страховка от бесконечного поворота), поэтому без повторов
    // камера останавливалась бы при зажатой кнопке.
    expect(moves.length).toBe(4);
  });

  it("отпускание кнопки останавливает и повтор, и камеру", async () => {
    const { hold, moves, stops } = makeHold();
    hold.start({ pan: 0.5 });
    hold.end();
    await vi.advanceTimersByTimeAsync(PTZ_REPEAT_MS * 3);
    expect(stops).toEqual([1]);
    expect(moves.length).toBe(1);   // после отпускания — ни одной новой команды
  });

  it("размонтирование во время удержания останавливает камеру", async () => {
    // Свернуть развёрнутую камеру в мозаику, не отпустив стрелку, — самый
    // простой способ оставить купол в движении: pointerup прилетит уже
    // мёртвому компоненту.
    const { hold, stops } = makeHold();
    hold.start({ tilt: -0.5 });
    hold.dispose();
    await vi.advanceTimersByTimeAsync(PTZ_REPEAT_MS);
    expect(stops).toEqual([1]);
    expect(hold.holding).toBe(false);
  });

  it("ошибка команды прекращает удержание и всё равно шлёт «стоп»", async () => {
    // Команда могла дойти до камеры и не вернуть ответ — камера в этом
    // случае уже поворачивается, и «стоп» нужен именно тогда.
    const { hold, stops } = makeHold({ move: () => Promise.reject(new Error("сеть")) });
    hold.start({ pan: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(stops).toEqual([1]);
    expect(hold.holding).toBe(false);
  });

  it("падение самого «стопа» не роняет пульт", async () => {
    const onError = vi.fn();
    const hold = new PtzHold({
      move: () => Promise.resolve(),
      stop: () => Promise.reject(new Error("сеть")),
      onError,
    });
    hold.start({ pan: 0.5 });
    hold.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(onError).toHaveBeenCalled();
  });

  it("повторный end() не шлёт второй «стоп»", async () => {
    // end() дёргается из трёх мест сразу (pointerup на кнопке, pointerup на
    // окне, pointerleave) — на каждое нажатие приходит несколько вызовов.
    const { hold, stops } = makeHold();
    hold.start({ pan: 0.5 });
    hold.end();
    hold.end();
    hold.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(stops).toEqual([1]);
  });

  it("end() без удержания не шлёт ничего", async () => {
    // Слушатель pointerup висит на окне и срабатывает на любой клик по
    // странице — «стоп» на каждый клик мимо пульта был бы потоком запросов
    // к камере на ровном месте.
    const { hold, stops, moves } = makeHold();
    hold.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(stops).toEqual([]);
    expect(moves).toEqual([]);
  });

  it("смена направления не гасит движение «стопом»", async () => {
    // Следующий ContinuousMove замещает скорость сам. «Стоп», посланный
    // перед ним, мог бы прийти к камере позже нового движения (независимые
    // соединения) и остановить её при зажатой кнопке.
    const { hold, moves, stops } = makeHold();
    hold.start({ pan: 0.5 });
    hold.start({ pan: -0.5 });
    await vi.advanceTimersByTimeAsync(0);
    expect(moves).toEqual([{ pan: 0.5 }, { pan: -0.5 }]);
    expect(stops).toEqual([]);
  });

  it("смена направления не оставляет два повторяющихся направления", async () => {
    const { hold, moves } = makeHold();
    hold.start({ pan: 0.5 });
    hold.start({ tilt: 0.5 });
    await vi.advanceTimersByTimeAsync(PTZ_REPEAT_MS);
    // Первое направление больше не повторяется — иначе камера дёргалась бы
    // между двумя осями.
    expect(moves.filter(m => m.pan !== undefined).length).toBe(1);
    expect(moves.filter(m => m.tilt !== undefined).length).toBe(2);
  });

  it("медленная камера не копит очередь команд", async () => {
    // Пока команда предыдущего тика не вернулась, следующая не уходит:
    // иначе «стоп» встал бы в конец очереди и пришёл к камере через
    // секунды после того, как кнопку отпустили.
    let resolveMove: (() => void) | null = null;
    const moves: any[] = [];
    const hold = new PtzHold({
      move: v => { moves.push(v); return new Promise<void>(res => { resolveMove = res; }); },
      stop: () => Promise.resolve(),
    });
    hold.start({ pan: 0.5 });
    await vi.advanceTimersByTimeAsync(PTZ_REPEAT_MS * 3);
    expect(moves.length).toBe(1);

    resolveMove!();
    await vi.advanceTimersByTimeAsync(PTZ_REPEAT_MS);
    expect(moves.length).toBe(2);
  });

  it("период повтора короче таймаута движения на камере", () => {
    // Инвариант между фронтендом и onvif_client.PTZ_MOVE_TIMEOUT_SEC (2 с):
    // если период станет длиннее таймаута, непрерывное движение начнёт
    // прерываться при зажатой кнопке.
    expect(PTZ_REPEAT_MS).toBeLessThan(2000);
  });
});
