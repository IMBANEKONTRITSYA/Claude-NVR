import { describe, expect, it } from "vitest";
import { backupHealth, formatBytes, formatStamp, stampOf, STALE_HOURS } from "./backupView";

// SPEC §11 «Резервное копирование: автоматическое раз в сутки + ручной
// запуск». Проверяется главным образом одно утверждение: карточка обязана
// отличать «копии есть» от «копии свежие». Каталог из четырнадцати дампов,
// последнему из которых неделя, выглядит благополучно ровно до дня
// восстановления — и именно так выглядел бы объект, на котором таймер
// бэкапа не отработал.

const ready = { ready: true, reason: null };

describe("здоровье бэкапа", () => {
  it("свежая копия — всё в порядке", () => {
    expect(backupHealth({ count: 3, latest_age_hours: 5, pg_dump: ready }).level).toBe("ok");
  });

  it("копии есть, но последней больше суток — это отказ, а не «ок»", () => {
    const h = backupHealth({ count: 14, latest_age_hours: 24 * 7, pg_dump: ready });
    expect(h.level).toBe("err");
    expect(h.text).toMatch(/7 дн/);
  });

  it("порог с запасом на разброс таймера, а не ровно 24 часа", () => {
    // RandomizedDelaySec=15m в facewatch-backup.timer плюс время самого
    // дампа: ровно 24 часа давали бы ложную тревогу каждое утро.
    expect(STALE_HOURS).toBeGreaterThan(24);
    expect(backupHealth({ count: 1, latest_age_hours: 25, pg_dump: ready }).level).toBe("ok");
    expect(backupHealth({ count: 1, latest_age_hours: STALE_HOURS + 1, pg_dump: ready }).level)
      .toBe("err");
  });

  it("ни одной копии — отказ с прямым текстом", () => {
    expect(backupHealth({ count: 0, latest_age_hours: null, pg_dump: ready }).level).toBe("err");
  });

  it("нерабочий pg_dump важнее возраста копий", () => {
    // Копии на диске могут быть свежими (их снял cron-контейнер), а кнопка
    // при этом не работает: администратор должен видеть причину сразу, а
    // не выяснять её нажатием.
    const h = backupHealth({
      count: 3, latest_age_hours: 1,
      pg_dump: { ready: false, reason: "pg_dump не найден" },
    });
    expect(h.level).toBe("err");
    expect(h.text).toBe("pg_dump не найден");
  });
});

describe("момент снятия — из имени файла", () => {
  it("разбирает имя, которое ставит бэкенд", () => {
    // Формат обязан совпадать с NAME_RE в backend/app/services/backup.py.
    const dt = stampOf("facewatch_20260822_031500.sql.gz");
    expect(dt?.toISOString()).toBe("2026-08-22T03:15:00.000Z");
  });

  it("чужое имя не притворяется копией", () => {
    expect(stampOf("somebody_elses.sql.gz")).toBeNull();
    expect(stampOf("facewatch_20260822_031500.sql.gz.part")).toBeNull();
    expect(formatStamp("README.txt")).toBe("README.txt");
  });
});

describe("размеры", () => {
  it("читаются человеком", () => {
    expect(formatBytes(0)).toBe("0 Б");
    expect(formatBytes(2048)).toBe("2.0 КБ");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 МБ");
    expect(formatBytes(null)).toBe("—");
  });
});
