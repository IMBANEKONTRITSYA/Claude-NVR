// SPEC §15 «Заметки, теги, watchlist». Чистая часть тегов персоны: разбор
// ввода оператора и приведение к тому же каноническому виду, что и на
// сервере (backend/app/services/person_tags.py).
//
// Зачем повторять нормализацию на клиенте, если её делает сервер: без неё
// чипы прыгают. Оператор набирает «VIP», карточка показывает «VIP», а
// после перезагрузки страницы — «vip»; тот же тег, набранный второй раз с
// другим регистром, до сохранения выглядит как второй чип и исчезает
// после. Правило одно и то же с обеих сторон, и расхождение ловится
// тестом на равенство пределов.
//
// Сервер остаётся источником истины: он отвечает 400 на превышение
// пределов, и его ответ показывается как есть. Клиент лишь не даёт
// довести до отказа молча.

export const MAX_TAG_LEN = 40;
export const MAX_TAGS_PER_PERSON = 20;

/** Один тег в каноническом виде; пустая строка означает «тега нет». */
export function normalizeTag(raw: string): string {
  return raw.trim().split(/\s+/).join(" ").toLowerCase();
}

/** Канонизация, дедупликация и сортировка — как на сервере. */
export function normalizeTags(raw: string[]): string[] {
  const seen = new Set<string>();
  for (const item of raw) {
    const tag = normalizeTag(item);
    if (tag) seen.add(tag);
  }
  // localeCompare, а не сортировка по кодовым точкам: на объекте теги
  // кириллические, и порядок «Ё после Я» оператор читает как поломку.
  return [...seen].sort((a, b) => a.localeCompare(b));
}

/**
 * Разбор строки ввода в теги. Разделители — запятая и перевод строки:
 * пробел разделителем быть не может, иначе «служба охраны» распалось бы
 * на два тега.
 */
export function parseTagInput(text: string): string[] {
  return normalizeTags(text.split(/[,\n]/));
}

/**
 * Итог правки списка тегов.
 *
 * Одна форма на успех и на отказ, а не размеченное объединение: в проекте
 * `strict: false`, и без strictNullChecks TypeScript не сужает union по
 * булеву полю — вызывающий код не собрался бы (проверено tsc). Поэтому
 * поля заполнены всегда: при отказе `tags` — прежний список, то есть
 * вызывающий, забывший проверить `ok`, сохранит то же, что было, а не
 * undefined.
 */
export type TagEditResult = { ok: boolean; tags: string[]; error: string };

/**
 * Добавление тега к списку. Возвращает либо новый список, либо причину
 * отказа — компонент показывает её оператору, а не отбрасывает ввод
 * молча (тот же принцип, что у галочки watchlist в Persons.tsx).
 */
export function addTag(current: string[], input: string): TagEditResult {
  const kept = normalizeTags(current);
  const refuse = (error: string): TagEditResult => ({ ok: false, tags: kept, error });

  const incoming = parseTagInput(input);
  if (incoming.length === 0) return refuse("Пустой тег");

  const tooLong = incoming.find(t => t.length > MAX_TAG_LEN);
  if (tooLong) {
    return refuse(`Тег длиннее ${MAX_TAG_LEN} символов: «${tooLong.slice(0, MAX_TAG_LEN)}…»`);
  }

  const merged = normalizeTags([...current, ...incoming]);
  if (merged.length > MAX_TAGS_PER_PERSON) {
    return refuse(`Не больше ${MAX_TAGS_PER_PERSON} тегов у персоны`);
  }
  return { ok: true, tags: merged, error: "" };
}

export function removeTag(current: string[], tag: string): string[] {
  const canon = normalizeTag(tag);
  return current.filter(t => normalizeTag(t) !== canon);
}
