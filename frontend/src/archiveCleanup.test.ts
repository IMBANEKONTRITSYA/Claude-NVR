import { describe, expect, it } from "vitest";
import { cleanupBanner, SLOW_PASS_SEC } from "./archiveCleanup";

describe("cleanupBanner", () => {
  it("отсутствие данных — не авария", () => {
    // Воркер прежней версии либо Redis пуст. Рисовать «уборка не работает»
    // значило бы объявить аварию по незнанию.
    expect(cleanupBanner(null)).toBeNull();
    expect(cleanupBanner(undefined)).toBeNull();
  });

  it("здоровая уборка молчит", () => {
    // Главное свойство: часовой проход, который отработал за полминуты, —
    // не новость. Баннер на каждый час приучили бы пролистывать, и тогда
    // его не заметят и в тот час, когда он важен.
    expect(cleanupBanner({ state: "idle", seconds: 3, skipped: 0 })).toBeNull();
    expect(cleanupBanner({
      state: "done", seconds: 1200, last_pass_sec: 31.4, skipped: 0,
    })).toBeNull();
  });

  it("короткий идущий проход тоже молчит", () => {
    expect(cleanupBanner({
      state: "running", seconds: SLOW_PASS_SEC - 1, skipped: 0,
    })).toBeNull();
  });

  it("затянувшийся проход — нейтральная строка, не авария", () => {
    const b = cleanupBanner({ state: "running", seconds: 1800, skipped: 0 })!;
    expect(b.tone).toBe("info");
    expect(b.title).toContain("30 мин");
    // Дежурный должен из строки понять, что запись при этом идёт: до
    // цикла 57 долгая уборка действительно останавливала соседей.
    expect(b.detail).toContain("§2");
  });

  it("упавший проход — авария с именами упавших проходов", () => {
    const b = cleanupBanner({
      state: "failed", seconds: 5, error: "cleanup_old", skipped: 0,
    })!;
    expect(b.tone).toBe("warn");
    expect(b.detail).toContain("cleanup_old");
  });

  it("упавший проход без текста ошибки всё равно показывается", () => {
    const b = cleanupBanner({ state: "failed", seconds: 5 })!;
    expect(b.tone).toBe("warn");
    expect(b.detail).toContain("журнале");
  });

  it("пропущенные проходы — авария даже при здоровом текущем состоянии", () => {
    // Это и есть будущий переполненный диск, видимый заранее: снаружи
    // состояние выглядит нормальным («done»), а архив уже чистится
    // медленнее, чем растёт.
    const b = cleanupBanner({
      state: "done", seconds: 10, last_pass_sec: 4000, skipped: 2,
    })!;
    expect(b.tone).toBe("warn");
    expect(b.title).toContain("2");
    expect(b.detail).toContain("§5");
  });

  it("отказ важнее темпа", () => {
    // Если уборка и падает, и не укладывается в час — чинить надо отказ.
    const b = cleanupBanner({
      state: "failed", seconds: 5, error: "prune_orphan_media", skipped: 3,
    })!;
    expect(b.detail).toContain("prune_orphan_media");
  });
});
