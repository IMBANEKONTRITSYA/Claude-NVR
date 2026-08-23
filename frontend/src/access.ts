/**
 * Матрица прав §18 одним местом — источник истины для меню и роутов.
 *
 * Находка цикла 51: до неё матрица существовала тремя независимыми копиями
 * — списком ролей в `<Private roles={...}>`, условием `can(...)` у пункта
 * меню и проверкой `require_role` на бэкенде, — и «Стена распознавания»
 * разошлась во всех трёх сразу: роут был открыт любой роли, пункт меню
 * рисовался всем, а лента `/api/events` отвечала любому аутентифицированному
 * пользователю. Наблюдатель, получавший 403 на «Карточках персон», читал их
 * содержимое (имя, кадр лица, watchlist-теги) со Стены.
 *
 * Разделы названы так же, как строки §18, чтобы сверка с ТЗ шла глазами по
 * одному списку, а не по трём файлам.
 */
export type Role = "admin" | "operator" | "viewer";

export const ADMIN_ONLY: readonly Role[] = ["admin"];
export const ANALYTICS_ROLES: readonly Role[] = ["admin", "operator"];
export const ALL_ROLES: readonly Role[] = ["admin", "operator", "viewer"];

/**
 * Раздел интерфейса → роли, которым §18 его разрешает.
 *
 * `wall` — модуль распознавания §15, а не наблюдения: Стена показывает имя,
 * хранимый кадр и watchlist-разметку, то есть карточку персоны. Строки
 * «Стена» в §18 нет, а §9 перечисляет содержимое дашборда поимённо
 * («количество событий, активность по часам, топ камер») и персон среди
 * него не значится — значит Стену покрывает строка «Карточки персон».
 *
 * `live` остаётся у всех: §4 прямо разрешает наблюдателю рамки лиц
 * «с подписями», а §18 даёт ему строку «Просмотр видео онлайн» целиком.
 */
export const SECTION_ROLES = {
  dashboard: ALL_ROLES,      // «Дашборд и мониторинг» — Да/Да/Да
  live: ALL_ROLES,           // «Просмотр видео онлайн» — Да/Да/Да
  profile: ALL_ROLES,        // собственный профиль — вне матрицы
  wall: ANALYTICS_ROLES,     // «Карточки персон» — Да/Да/Нет
  persons: ANALYTICS_ROLES,  // «Карточки персон» — Да/Да/Нет
  search: ANALYTICS_ROLES,   // «Поиск по фото (аналитика)» — Да/Да/Нет
  roi: ANALYTICS_ROLES,      // «Настройка ROI» — Да/Да/Нет
  archive: ANALYTICS_ROLES,  // «Архив и экспорт» — Да/Да/Нет
  reports: ANALYTICS_ROLES,  // «Отчёты» — Да/Да/Нет
  monitoring: ANALYTICS_ROLES, // «Системный мониторинг» — Да/Ограниченно/Нет
  cameras: ADMIN_ONLY,       // «Управление камерами и режимами» — Да/Нет/Нет
  users: ADMIN_ONLY,         // «Управление пользователями» — Да/Нет/Нет
  settings: ADMIN_ONLY,      // «Настройка retention», «Смена профиля» — Да/Нет/Нет
  audit: ADMIN_ONLY,         // «Просмотр логов аудита» — Да/Нет/Нет
} as const;

export type Section = keyof typeof SECTION_ROLES;

/** Разрешает ли §18 роли `role` раздел `section`. */
export function canAccess(section: Section, role: string): boolean {
  return (SECTION_ROLES[section] as readonly string[]).includes(role);
}

/**
 * Роли, которым доступны данные карточек персон (имя, кадр лица, теги).
 *
 * Отдельный предикат, а не `canAccess("persons", ...)`: им проверяется не
 * раздел меню, а право видеть конкретные поля — блок «Топ-10 персон» на
 * дашборде, который сам по себе разделом не является.
 */
export function canSeePersonIdentity(role: string): boolean {
  return (ANALYTICS_ROLES as readonly string[]).includes(role);
}
