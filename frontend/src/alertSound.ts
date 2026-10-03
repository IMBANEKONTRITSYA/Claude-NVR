/**
 * Звуковое оповещение о персоне из watchlist (SPEC §6 «Алерты при детекции
 * (Telegram, email, звук)»).
 *
 * Сигнал синтезируется WebAudio, а не проигрывается файлом: nginx отдаёт
 * статику под строгим CSP (`default-src 'self'`), и любой внешний звук
 * пришлось бы класть в сборку отдельным ассетом ради двух гудков. Заодно
 * это снимает вопрос лицензии на семпл (§24 — только разрешительные).
 */

/** Не чаще одного сигнала за этот интервал. */
export const BEEP_THROTTLE_MS = 5000;

/**
 * Решает, подавать ли сигнал сейчас.
 *
 * Тротлинг обязателен и не совпадает с cooldown'ом воркера: тот считается
 * ПО ПЕРСОНЕ (`alert_cooldown:<person_id>`), поэтому проход пяти разных
 * watchlist-персон в одну секунду — пять законных алертов и пять
 * накладывающихся гудков. Здесь тротлинг общий по вкладке.
 *
 * Состояние передаётся аргументом, а не хранится в модуле, чтобы решение
 * можно было проверить тестом без таймеров и без WebAudio.
 */
export function shouldBeep(lastBeepAt: number, now: number): boolean {
  return now - lastBeepAt >= BEEP_THROTTLE_MS;
}

/** Событие Стены, по которому положено подать сигнал. */
export function isAlertEvent(msg: any): boolean {
  return msg?.type === "face" && msg?.alert === true;
}

type Ctor = { new (): AudioContext };

let ctx: AudioContext | null = null;

function audioContext(): AudioContext | null {
  if (ctx) return ctx;
  const C: Ctor | undefined =
    (window as any).AudioContext || (window as any).webkitAudioContext;
  if (!C) return null;
  try {
    ctx = new C();
  } catch {
    return null;
  }
  return ctx;
}

/**
 * Два коротких гудка. Ошибки намеренно проглатываются: браузер блокирует
 * звук до первого взаимодействия со страницей (autoplay policy), и
 * оператор, открывший Стену на видеостене и не трогавший мышь, не должен
 * получить вместо алерта пустой экран из-за необработанного исключения.
 */
export function playAlertBeep(): void {
  const ac = audioContext();
  if (!ac) return;
  try {
    if (ac.state === "suspended") void ac.resume();
    const now = ac.currentTime;
    for (const offset of [0, 0.18]) {
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      // Плавное затухание вместо резкого stop(): обрыв синусоиды на
      // ненулевой амплитуде даёт щелчок, который на громкой видеостене
      // слышен отчётливее самого сигнала.
      gain.gain.setValueAtTime(0.0001, now + offset);
      gain.gain.exponentialRampToValueAtTime(0.25, now + offset + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.14);
      osc.connect(gain).connect(ac.destination);
      osc.start(now + offset);
      osc.stop(now + offset + 0.15);
    }
  } catch {
    /* звук — вспомогательный канал, его отказ не ломает Стену */
  }
}
