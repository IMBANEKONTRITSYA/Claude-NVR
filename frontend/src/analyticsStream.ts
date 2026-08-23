/**
 * Как показать в §9-мониторинге поток, по которому идёт аналитика.
 *
 * SPEC §2 и §15 называют допустимым источником кадров основной поток либо
 * субпоток «с разрешением не ниже 640×480». Воркер выбирает поток по
 * фактически измеренному кадру (worker/analytics_source.py) и публикует
 * решение вместе с причиной; здесь причина превращается в подпись и цвет.
 *
 * Вынесено из Monitoring.tsx отдельным модулем, чтобы решение проверялось
 * юнит-тестами без рендера страницы: набор причин задаёт воркер, и
 * разъезжаться две стороны не должны.
 */

export type AnalyticsSource = {
  stream?: string | null;
  reason?: string | null;
  width?: number | null;
  height?: number | null;
  note?: string | null;
};

export type StreamTone = "ok" | "warn" | "muted";

export type StreamCell = {
  label: string;
  tone: StreamTone;
  note: string;
};

/**
 * Подпись потока: «Субпоток 704×576» / «Основной поток».
 *
 * Разрешение приписывается ТОЛЬКО к субпотоку, и это не косметика.
 * Воркер отдаёт ширину и высоту субпотока даже тогда, когда сам увёл
 * аналитику на основной поток, — числа объясняют решение и обязаны быть в
 * ответе. Но приписать их к подписи «Основной поток» значило бы заявить,
 * что основной поток камеры имеет разрешение 352×288, чего никто не
 * измерял и что почти наверняка неверно. Числа субпотока живут в
 * пояснении (`note`), где сказано, чьи они.
 */
export function streamLabel(src: AnalyticsSource): string {
  if (src.stream === "sub") {
    return src.width && src.height
      ? `Субпоток ${src.width}×${src.height}`
      : "Субпоток";
  }
  return "Основной поток";
}

/**
 * Цвет строки. Предупреждением помечаются ровно те случаи, где система
 * работает не так, как задумана схемой двух потоков:
 *
 *  - sub_below_floor — субпоток не дотянул до порога, аналитика уехала на
 *    основной поток; это кратно более дорогой декод, и дежурный должен
 *    знать, что камера теперь стоит серверу больше;
 *  - sub_below_floor_no_main — уходить было некуда, детекция идёт по
 *    кадру ниже порога §15, то есть качество распознавания ухудшено;
 *  - sub_resolution_unknown — разрешение не измерилось, порог не
 *    проверен: не поломка, но и не «всё в порядке».
 *
 * Штатные sub_meets_floor и no_sub предупреждения не требуют.
 */
export function streamTone(src: AnalyticsSource): StreamTone {
  switch (src.reason) {
    case "sub_below_floor":
    case "sub_below_floor_no_main":
      return "warn";
    case "sub_resolution_unknown":
      return "muted";
    default:
      return "ok";
  }
}

export function streamCell(src: AnalyticsSource): StreamCell {
  return { label: streamLabel(src), tone: streamTone(src), note: src.note || "" };
}
