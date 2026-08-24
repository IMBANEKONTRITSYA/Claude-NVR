// SPEC §3: «Группировка/пагинация для больших объектов (100+ камер)».
//
// Из этой строки на странице «Камеры» до цикла 63 был только текстовый
// фильтр — и это было осознанным решением («на 120 камерах нужную ищут по
// имени, а не листают страницы»), но фильтр закрывает поиск одной камеры,
// а не группировку парка. На объекте из 250 камер вопрос оператора чаще
// звучит как «покажи корпус 7», а не «покажи камеру 137»: локация — это
// то, чем парк размечен физически, и группа по ней отвечает на него одним
// кликом, тогда как текстовый фильтр требует помнить точное написание.
//
// Замерено на живом стенде (250 камер, настоящий nginx + браузер): список
// целиком — 84.7 КБ и ~9 мс на стороне API, страница открывается за 555 мс
// и рисует все 250 строк. То есть проблема не в скорости, а в том, что
// парк нечем разложить по объекту, — поэтому здесь группировка, а не
// пагинация: она отвечает на реальный вопрос оператора и не отменяет
// прежнее решение о фильтре, а дополняет его.
//
// Логика вынесена из компонента отдельным модулем ради теста: раскладка
// парка — это правила (пустая локация, порядок, регистр), а не разметка.

/** Камера в том виде, в каком её отдаёт GET /api/cameras. */
export type CameraLike = { id: number; name?: string; location?: string | null };

/** Ярлык группы «локация не заполнена» — одна на все места интерфейса. */
export const NO_LOCATION = "— без локации —";

/** Значение селектора «показать все группы». */
export const ALL_LOCATIONS = "";

export type LocationGroup = { location: string; count: number };

/** Ключ группы: пустая и незаполненная локация — одна и та же группа. */
export function groupKey(location: string | null | undefined): string {
  const trimmed = (location || "").trim();
  return trimmed || NO_LOCATION;
}

/**
 * Группы локаций с числом камер в каждой.
 *
 * Порядок — по имени, но группа «без локации» всегда последняя: это не
 * место на объекте, а признак незаполненной карточки, и в середине списка
 * она мешала бы читать раскладку по корпусам.
 */
export function locationGroups(cams: CameraLike[]): LocationGroup[] {
  const counts = new Map<string, number>();
  for (const cam of cams) {
    const key = groupKey(cam.location);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const groups = [...counts.entries()].map(([location, count]) => ({ location, count }));
  groups.sort((a, b) => {
    if (a.location === NO_LOCATION) return 1;
    if (b.location === NO_LOCATION) return -1;
    // localeCompare, а не сравнение строк: «Корпус 10» иначе встаёт между
    // «Корпус 1» и «Корпус 2».
    return a.location.localeCompare(b.location, "ru", { numeric: true });
  });
  return groups;
}

/**
 * Отбор камер группой и текстовым фильтром — оба применяются вместе.
 *
 * Порядок камер сохраняется: список приходит отсортированным по id, и
 * оператор, привыкший к позиции камеры, не должен терять её при выборе
 * группы.
 */
export function filterCameras<T extends CameraLike>(
  cams: T[], location: string, text: string,
): T[] {
  const needle = text.trim().toLowerCase();
  return cams.filter(cam => {
    if (location !== ALL_LOCATIONS && groupKey(cam.location) !== location) return false;
    if (!needle) return true;
    return `${cam.id} ${cam.name || ""} ${cam.location || ""}`.toLowerCase().includes(needle);
  });
}
