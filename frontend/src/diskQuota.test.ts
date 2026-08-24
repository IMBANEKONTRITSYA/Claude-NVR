import { describe, expect, it } from "vitest";
import { quotaBanner, SLOW_PASS_SEC } from "./diskQuota";
import { cleanupBanner } from "./archiveCleanup";

describe("quotaBanner", () => {
  it("отсутствие данных — не авария", () => {
    // Воркер прежней версии либо Redis пуст. Рисовать «перезапись не
    // работает» значило бы объявить аварию по незнанию.
    expect(quotaBanner(null)).toBeNull();
    expect(quotaBanner(undefined)).toBeNull();
  });

  it("здоровая перезапись молчит", () => {
    // На незаполненном диске проход — это один statvfs и выход: он идёт
    // каждые ~10 с и не новость ни в каком виде.
    expect(quotaBanner({ state: "idle", seconds: 3, skipped: 0 })).toBeNull();
    expect(quotaBanner({
      state: "done", seconds: 8, last_pass_sec: 0.1, skipped: 0,
    })).toBeNull();
  });

  it("короткий идущий проход тоже молчит", () => {
    expect(quotaBanner({
      state: "running", seconds: SLOW_PASS_SEC - 1, skipped: 0,
    })).toBeNull();
  });

  it("пропуски НЕ поднимают тревогу — в отличие от уборки", () => {
    // Главное отличие двух правил показа, и оно не косметическое.
    // Перезапись запрашивается каждым проходом менеджера (~10 с), поэтому
    // любой проход длиннее одного прохода менеджера даёт пропуск на
    // следующем. На переполненном томе — а это ровно тот случай, когда
    // перезапись вообще работает, — пропуски идут подряд и означают штатный
    // режим. Тревога по ним не гасла бы никогда, то есть перестала бы
    // что-либо значить.
    //
    // Проверка откатом: скопируйте в diskQuota.ts ветку `skipped > 0` из
    // archiveCleanup.ts — этот тест упадёт.
    expect(quotaBanner({ state: "running", seconds: 12, skipped: 40 })).toBeNull();
    expect(quotaBanner({
      state: "done", seconds: 2, last_pass_sec: 14, skipped: 137,
    })).toBeNull();

    // Контроль на том же входе: у уборки тот же признак — тревога. Если обе
    // ветки однажды сведут к общей, упадёт ровно эта строка.
    expect(cleanupBanner({ state: "running", seconds: 12, skipped: 40 }))
      .not.toBeNull();
  });

  it("затянувшийся проход — нейтральная строка, не авария", () => {
    const b = quotaBanner({ state: "running", seconds: 1800, skipped: 3 })!;
    expect(b.tone).toBe("info");
    expect(b.title).toContain("30 мин");
    // Дежурный должен из строки понять, что запись при этом идёт: до этого
    // цикла долгая перезапись действительно останавливала контур записи.
    expect(b.detail).toContain("§2");
  });

  it("упавший проход — авария с именем упавшего этапа", () => {
    const b = quotaBanner({
      state: "failed", seconds: 5, error: "enforce_disk_quota", skipped: 0,
    })!;
    expect(b.tone).toBe("warn");
    expect(b.detail).toContain("enforce_disk_quota");
    // Отказ перезаписи весит больше отказа уборки: это последнее, что не
    // даёт записи встать на переполненном томе. Строка обязана это назвать,
    // иначе дежурный отложит её как «место кончается, разберусь завтра».
    expect(b.detail).toContain("запись");
  });

  it("упавший проход без текста ошибки всё равно показывается", () => {
    const b = quotaBanner({ state: "failed", seconds: 5, skipped: 0 })!;
    expect(b.tone).toBe("warn");
    expect(b.detail).toContain("журнале воркера");
  });

  it("отказ показывается даже при пропусках — чинить надо отказ", () => {
    const b = quotaBanner({
      state: "failed", seconds: 5, error: "enforce_disk_quota", skipped: 12,
    })!;
    expect(b.tone).toBe("warn");
  });
});
