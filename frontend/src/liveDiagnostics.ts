/**
 * Почему плитка стены чёрная (SPEC §4).
 *
 * До этого модуля живой просмотр умел ровно два ответа: картинка либо
 * «Камера недоступна». Под этой надписью прячется весь набор причин, и
 * различить их без доступа к серверу нельзя — а именно это и требовалось
 * дежурному, когда почернела вся стена сразу.
 *
 * Причины разные, и действия по ним разные:
 *
 * * плейлист не отдаётся (401/403) — истекла сессия просмотра, лечится
 *   перезагрузкой страницы, камера ни при чём;
 * * плейлист не отдаётся (404) — медиасервер не знает такого пути, то
 *   есть слой записи не завёл камеру; смотреть надо в «Мониторинг», и
 *   почернеют при этом ВСЕ камеры сразу;
 * * плейлист отдаётся, но не играет — почти всегда кодек: §1 допускает
 *   камеры с H.265, а HLS с H.265 воспроизводит Safari и не воспроизводят
 *   Chrome и Firefox. Симптом при этом обманчив: статус камеры зелёный,
 *   архив растёт, а плитка чёрная.
 *
 * Модуль — чистая функция над кодом ответа и текстом плейлиста, без
 * fetch и без DOM: разбор плейлиста проверяется в vitest на настоящих
 * ответах MediaMTX v1.16.0, снятых с работающего сервера.
 */

/** Кодеки, которые MediaMTX объявляет в мастер-плейлисте для H.265. */
const HEVC_PREFIXES = ["hvc1", "hev1"];

export interface LiveFailure {
  /** Код ответа на мастер-плейлист; 0 — до сервера не дошли вовсе. */
  playlistStatus: number;
  /** Тело мастер-плейлиста, если оно получено. */
  playlistBody?: string;
}

export interface LiveDiagnosis {
  /** Короткая надпись на плитке. */
  title: string;
  /** Что делать — строкой ниже, мельче. Пусто, если добавить нечего. */
  hint: string;
}

/**
 * `CODECS="hvc1.1.6.L93.90,mp4a.40.2"` → `["hvc1.1.6.L93.90", "mp4a.40.2"]`.
 *
 * Значение атрибута всегда в кавычках (RFC 8216 §4.2: список через
 * запятую — quoted-string), поэтому запятая внутри кавычек разделяет
 * кодеки, а не атрибуты.
 */
export function playlistCodecs(playlist: string): string[] {
  const out: string[] = [];
  for (const m of playlist.matchAll(/CODECS="([^"]*)"/g)) {
    for (const codec of m[1].split(",")) {
      const trimmed = codec.trim();
      if (trimmed) out.push(trimmed);
    }
  }
  return out;
}

/** Есть ли в плейлисте видеодорожка H.265. */
export function hasHevc(playlist: string): boolean {
  return playlistCodecs(playlist).some(c =>
    HEVC_PREFIXES.some(p => c.toLowerCase().startsWith(p)));
}

/**
 * Умеет ли браузер воспроизводить H.265 в fMP4.
 *
 * Спрашивается у самого браузера, а не выводится из user-agent: HEVC в
 * Chrome зависит от платформы и аппаратного декодера, и угадать это по
 * строке версии нельзя. `canPlayType` возвращает `"probably"`,
 * `"maybe"` или пустую строку.
 */
export function browserPlaysHevc(
  canPlayType: (type: string) => string = defaultCanPlayType,
): boolean {
  return HEVC_PREFIXES.some(p => canPlayType(`video/mp4; codecs="${p}.1.6.L93.90"`) !== "");
}

function defaultCanPlayType(type: string): string {
  if (typeof document === "undefined") return "";
  return document.createElement("video").canPlayType(type);
}

export function diagnoseLiveFailure(
  failure: LiveFailure,
  canPlayType?: (type: string) => string,
): LiveDiagnosis {
  const { playlistStatus, playlistBody } = failure;

  if (playlistStatus === 0) {
    return {
      title: "Медиасервер недоступен",
      hint: "Плейлист не отдаётся вовсе. Проверьте раздел «Мониторинг» — карточка «Слой записи».",
    };
  }
  if (playlistStatus === 401 || playlistStatus === 403) {
    return {
      title: "Нет доступа к видеопотоку",
      hint: "Сессия просмотра истекла — обновите страницу. Камера при этом исправна.",
    };
  }
  if (playlistStatus === 404) {
    return {
      title: "Поток не заведён в медиасервере",
      hint: "Слой записи не синхронизировал камеру. Если чёрные все камеры сразу — смотрите «Мониторинг», карточку «Слой записи».",
    };
  }
  if (playlistStatus >= 500) {
    return {
      title: "Ошибка медиасервера",
      hint: `Плейлист отдан с кодом ${playlistStatus}.`,
    };
  }
  if (playlistStatus === 200 && playlistBody && hasHevc(playlistBody)
      && !browserPlaysHevc(canPlayType)) {
    return {
      title: "Браузер не воспроизводит H.265",
      hint: "Камера отдаёт поток в H.265, а этот браузер его не декодирует. Запись и архив при этом идут штатно; для просмотра подойдёт Safari либо перевод основного потока камеры в H.264.",
    };
  }
  if (playlistStatus === 200) {
    return {
      title: "Поток не воспроизводится",
      hint: "Плейлист отдаётся, но кадры не приходят: поток от камеры прервался либо не поддержан формат.",
    };
  }
  return {
    title: "Камера недоступна",
    hint: `Плейлист отдан с кодом ${playlistStatus}.`,
  };
}
