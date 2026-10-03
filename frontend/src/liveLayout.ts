/**
 * Раскладка стены live-просмотра и цифровой зум (SPEC §4).
 *
 * Вынесено из компонента отдельным модулем ради двух вещей.
 *
 * **Ограничение числа плиток — это не косметика, а условие работы на
 * объекте.** §1 новой редакции ТЗ задаёт 12–250+ камер, а §4 требует
 * «грид: 1, 4, 9, 16 камер на экран». До этого `LiveGrid` рендерил
 * **все** камеры разом: на объекте из ста камер вкладка поднимала сто
 * HLS-плееров, каждый со своим буфером и своим соединением к MediaMTX, —
 * браузер вставал раньше, чем сервер. Отбор страницы делается здесь,
 * чтобы одно и то же ограничение можно было проверить тестом, а не
 * глазами.
 *
 * **Цифровой зум (§4) обязан удерживать кадр в плитке.** Оператор тянет
 * увеличенное изображение мышью, и без ограничения смещения кадр
 * улетает за край, оставляя пустое поле, — состояние, из которого без
 * кнопки «сброс» не выбраться.
 */

/** Допустимые размеры мозаики (SPEC §4). */
export const LAYOUTS = [1, 4, 9, 16] as const;
export type Layout = (typeof LAYOUTS)[number];

export const DEFAULT_LAYOUT: Layout = 4;

/** Пределы цифрового зума: 1× — без увеличения, 8× — предел различимости
 *  на потоке 720p (§1: основной поток 1280×720). */
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 8;

/** Число колонок мозаики: 1→1, 4→2, 9→3, 16→4. */
export function gridColumns(layout: Layout): number {
  return Math.round(Math.sqrt(layout));
}

export function isLayout(v: unknown): v is Layout {
  return LAYOUTS.includes(v as Layout);
}

/** Сколько страниц занимает список камер при данной раскладке. */
export function pageCount(total: number, layout: Layout): number {
  return Math.max(1, Math.ceil(total / layout));
}

/** Номер страницы, приведённый к существующим (нумерация с 1).
 *
 *  Нужен при смене раскладки: страница 7 из 16 камер по 4 на экран не
 *  существует, если переключиться на 16 на экран, — без приведения стена
 *  оказалась бы пустой. */
export function clampPage(page: number, total: number, layout: Layout): number {
  const pages = pageCount(total, layout);
  if (!Number.isFinite(page)) return 1;
  return Math.min(Math.max(1, Math.trunc(page)), pages);
}

/** Камеры, попадающие на страницу. Только они получают HLS-плеер. */
export function pageSlice<T>(cams: T[], page: number, layout: Layout): T[] {
  const p = clampPage(page, cams.length, layout);
  return cams.slice((p - 1) * layout, (p - 1) * layout + layout);
}

export function clampZoom(z: number): number {
  if (!Number.isFinite(z)) return MIN_ZOOM;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
}

/**
 * Ограничение смещения кадра при зуме.
 *
 * Плитка считается единицей: при `scale(z)` относительно центра кадр
 * занимает `[-(z-1)/2, 1+(z-1)/2]`, поэтому смещение больше `(z-1)/2` по
 * модулю открыло бы пустое поле с одной из сторон. При `z = 1` предел
 * равен нулю — двигать нечего.
 */
export function clampPan(offset: number, zoom: number): number {
  const limit = (clampZoom(zoom) - 1) / 2;
  if (!Number.isFinite(offset)) return 0;
  return Math.min(limit, Math.max(-limit, offset));
}

export type Pan = { x: number; y: number };

/** Новое состояние зума с сохранением уже выбранного положения кадра. */
export function applyZoom(zoom: number, pan: Pan, factor: number): { zoom: number; pan: Pan } {
  const next = clampZoom(zoom * factor);
  return { zoom: next, pan: { x: clampPan(pan.x, next), y: clampPan(pan.y, next) } };
}

/** CSS-трансформация плитки: сдвиг в долях размера плитки, затем масштаб. */
export function zoomTransform(zoom: number, pan: Pan): string {
  if (zoom <= MIN_ZOOM) return "none";
  return `translate(${pan.x * 100}%, ${pan.y * 100}%) scale(${zoom})`;
}
