/**
 * Диагностика чёрной плитки стены (SPEC §4).
 *
 * Плейлисты в тестах — не выдуманные: сняты с настоящего MediaMTX
 * v1.16.0 (той версии, что зафиксирована в docker-compose.yml), который
 * тянул по RTSP камеру-двойник, кодированную в H.264 и в H.265
 * соответственно. Разбирать самодельную строку смысла нет: проверять
 * надо ровно ту форму, которая приедет в браузер.
 */
import { describe, expect, it } from "vitest";

import {
  browserPlaysHevc,
  diagnoseLiveFailure,
  hasHevc,
  playlistCodecs,
} from "./liveDiagnostics";

const H264 = `#EXTM3U
#EXT-X-VERSION:9
#EXT-X-INDEPENDENT-SEGMENTS

#EXT-X-STREAM-INF:BANDWIDTH=4544788,AVERAGE-BANDWIDTH=4544788,CODECS="avc1.42c01f",RESOLUTION=1280x720,FRAME-RATE=15.000
video1_stream.m3u8
`;

const H265 = `#EXTM3U
#EXT-X-VERSION:9
#EXT-X-INDEPENDENT-SEGMENTS

#EXT-X-STREAM-INF:BANDWIDTH=1523360,AVERAGE-BANDWIDTH=1523360,CODECS="hvc1.1.6.L93.90",RESOLUTION=1280x720,FRAME-RATE=15.000
video1_stream.m3u8
`;

const never = () => "";
const always = () => "probably";

describe("разбор плейлиста", () => {
  it("достаёт кодеки из настоящего плейлиста MediaMTX", () => {
    expect(playlistCodecs(H264)).toEqual(["avc1.42c01f"]);
    expect(playlistCodecs(H265)).toEqual(["hvc1.1.6.L93.90"]);
  });

  it("разделяет несколько кодеков одной дорожки по запятой внутри кавычек", () => {
    const withAudio = H265.replace('CODECS="hvc1.1.6.L93.90"',
      'CODECS="hvc1.1.6.L93.90,mp4a.40.2"');
    expect(playlistCodecs(withAudio)).toEqual(["hvc1.1.6.L93.90", "mp4a.40.2"]);
  });

  it("отличает H.265 от H.264", () => {
    expect(hasHevc(H265)).toBe(true);
    expect(hasHevc(H264)).toBe(false);
  });

  it("узнаёт и hev1, и hvc1 — MediaMTX объявляет H.265 обоими", () => {
    expect(hasHevc('#EXT-X-STREAM-INF:CODECS="hev1.1.6.L93.90"')).toBe(true);
  });

  it("на плейлисте без CODECS не выдумывает кодеков", () => {
    expect(playlistCodecs("#EXTM3U\n#EXT-X-VERSION:9\n")).toEqual([]);
    expect(hasHevc("#EXTM3U\n")).toBe(false);
  });
});

describe("поддержка H.265 браузером", () => {
  it("спрашивает браузер, а не user-agent", () => {
    expect(browserPlaysHevc(never)).toBe(false);
    expect(browserPlaysHevc(always)).toBe(true);
    // "maybe" — тоже поддержка: браузер не отказывается.
    expect(browserPlaysHevc(() => "maybe")).toBe(true);
  });
});

describe("диагноз чёрной плитки", () => {
  it("401 — истекла сессия просмотра, а не поломка камеры", () => {
    const d = diagnoseLiveFailure({ playlistStatus: 401 });
    expect(d.title).toBe("Нет доступа к видеопотоку");
    expect(d.hint).toContain("обновите страницу");
  });

  it("404 — камеру не завёл слой записи, и чёрными будут все сразу", () => {
    const d = diagnoseLiveFailure({ playlistStatus: 404 });
    expect(d.title).toContain("не заведён");
    expect(d.hint).toContain("Мониторинг");
  });

  it("недоступный сервер отличается от отказа сервера", () => {
    expect(diagnoseLiveFailure({ playlistStatus: 0 }).title).toBe("Медиасервер недоступен");
    expect(diagnoseLiveFailure({ playlistStatus: 503 }).title).toBe("Ошибка медиасервера");
  });

  it("плейлист есть, кодек H.265, браузер не умеет — называет причину", () => {
    const d = diagnoseLiveFailure({ playlistStatus: 200, playlistBody: H265 }, never);
    expect(d.title).toBe("Браузер не воспроизводит H.265");
    // Дежурный обязан понять, что архив при этом цел: иначе он полезет
    // чинить запись, с которой всё в порядке.
    expect(d.hint).toContain("архив");
  });

  it("тот же H.265 в браузере, который его умеет, на кодек не сваливает", () => {
    const d = diagnoseLiveFailure({ playlistStatus: 200, playlistBody: H265 }, always);
    expect(d.title).not.toContain("H.265");
    expect(d.title).toBe("Поток не воспроизводится");
  });

  it("H.264 не объявляется проблемой кодека даже в браузере без HEVC", () => {
    const d = diagnoseLiveFailure({ playlistStatus: 200, playlistBody: H264 }, never);
    expect(d.title).toBe("Поток не воспроизводится");
  });

  it("у каждого диагноза есть подсказка — иначе он не лучше «недоступна»", () => {
    const cases = [0, 401, 403, 404, 500, 200, 418];
    for (const playlistStatus of cases) {
      const d = diagnoseLiveFailure({ playlistStatus, playlistBody: H264 }, never);
      expect(d.title.length).toBeGreaterThan(0);
      expect(d.hint.length).toBeGreaterThan(0);
    }
  });
});
