/**
 * Состояние загрузки модели распознавания на странице §9-мониторинга.
 *
 * До цикла 55 страница знала о слое аналитики одно: `model_ready`. Пока
 * загрузка шла синхронно в нити менеджера, этого хватало — состояния
 * «грузится» снаружи фактически не существовало: воркер в этот момент не
 * публиковал вообще ничего, потому что публикация стояла в том же цикле,
 * что и загрузка.
 *
 * С переносом загрузки в отдельную нить (worker/model_loader.py, SPEC §2)
 * «грузится» стало обычным и длительным состоянием: запись уже идёт,
 * аналитика ещё поднимается, и на свежей установке это законные минуты.
 * Показывать его прежней оранжевой аварией «модель не загружена» значило
 * бы звать дежурного чинить то, что просто ещё качается, — а через минуту
 * само пройдёт. Обратная ошибка не лучше: молчать о загрузке нельзя, иначе
 * «аналитика ещё поднимается» и «аналитика не поднялась» снова
 * неразличимы.
 *
 * Поэтому три исхода, а не два: готово (баннера нет), грузится (нейтральная
 * строка со временем), отказ (авария с причиной).
 *
 * Вынесено из Monitoring.tsx отдельным модулем, чтобы решение проверялось
 * юнит-тестами без рендера страницы.
 */

export type ModelLoadState = "idle" | "loading" | "ready" | "error";

export type ModelLoad = {
  state?: ModelLoadState | null;
  seconds?: number | null;
  model?: string | null;
  error?: string | null;
};

export type Analytics = {
  model_ready?: boolean | null;
  model?: string | null;
  error?: string | null;
  load?: ModelLoad | null;
};

export type ModelBanner = {
  tone: "warn" | "info";
  title: string;
  detail: string | null;
  hint: string | null;
};

/** Человеческая длительность: «40 с», «3 мин». */
export function humanDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} с`;
  return `${Math.round(s / 60)} мин`;
}

const OFFLINE_HINT =
  "При первом запуске модель скачивается из интернета. На сервере без "
  + "доступа в сеть положите её в том insightface-models — см. INSTALL.";

/**
 * Баннер состояния аналитики, либо null — показывать нечего.
 *
 * `null` возвращается ровно в двух случаях: модель готова и данных о слое
 * аналитики нет вовсе (воркер старой версии либо Redis пуст). Второе — не
 * повод рисовать аварию: отсутствие данных о загрузке не значит, что она
 * провалилась.
 */
export function modelBanner(a: Analytics | null | undefined): ModelBanner | null {
  if (!a) return null;
  if (a.model_ready) return null;

  const load = a.load || {};
  const name = load.model || a.model || "";

  if (load.state === "loading") {
    const waited = typeof load.seconds === "number" ? humanDuration(load.seconds) : null;
    return {
      tone: "info",
      title: name
        ? `Модель «${name}» загружается${waited ? ` — ${waited}` : ""}.`
        : `Модель распознавания загружается${waited ? ` — ${waited}` : ""}.`,
      detail: "Запись идёт независимо от этого (SPEC §2): "
        + "распознавание включится само, как только модель поднимется.",
      // Подсказка про изолированный сервер уместна и здесь: если загрузка
      // висит десяток минут, это ровно тот случай, и ждать её конца, чтобы
      // об этом узнать, незачем.
      hint: typeof load.seconds === "number" && load.seconds > 120 ? OFFLINE_HINT : null,
    };
  }

  return {
    tone: "warn",
    title: `Распознавание лиц выключено: модель «${name}» не загружена. `
      + "Запись при этом идёт нормально.",
    detail: load.error || a.error || null,
    hint: OFFLINE_HINT,
  };
}
