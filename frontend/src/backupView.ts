// SPEC §11 «Резервное копирование: автоматическое раз в сутки + ручной
// запуск». Чистая часть карточки бэкапов: форматирование и — главное —
// решение о том, здоров ли автоматический бэкап.
//
// Вынесено из компонента отдельным модулем, потому что именно это решение
// и стоит проверять тестом: «копии есть» и «копии свежие» — разные
// утверждения, и администратор объекта должен видеть второе. Список из
// четырнадцати дампов, последнему из которых неделя, выглядит в интерфейсе
// благополучно ровно до того дня, когда он понадобится.

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} Б`;
  const units = ["КБ", "МБ", "ГБ", "ТБ"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  // Десятая доля — только у малых чисел: «2.0 КБ» полезно, «734.2 МБ» —
  // шум. Байты сюда не доходят вовсе (ветка выше).
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

// Момент снятия — из имени файла (facewatch_YYYYMMDD_HHMMSS.sql.gz), а не
// из created_at: имя ставит тот, кто снял дамп, а mtime меняется от
// копирования каталога на другой диск — то есть ровно от того, что
// администратор с бэкапами и делает.
export function stampOf(name: string): Date | null {
  const m = /^facewatch_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.sql\.gz$/.exec(name);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  return new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +s));
}

export function formatStamp(name: string): string {
  const dt = stampOf(name);
  if (!dt) return name;
  return dt.toLocaleString();
}

export type BackupHealth = {
  level: "ok" | "warn" | "err";
  text: string;
};

// Порог тревоги — сутки плюс запас на разброс таймера (RandomizedDelaySec
// в facewatch-backup.timer — 15 минут) и на длительность самого дампа.
// Ровно 24 часа давали бы ложную тревогу каждое утро.
export const STALE_HOURS = 30;

export function backupHealth(status: {
  count?: number;
  latest_age_hours?: number | null;
  pg_dump?: { ready?: boolean; reason?: string | null };
}): BackupHealth {
  const probe = status.pg_dump || {};
  if (probe.ready === false) {
    return { level: "err", text: probe.reason || "pg_dump недоступен — копии не снимаются" };
  }
  if (!status.count) {
    return { level: "err", text: "Ни одной копии. Автоматический бэкап (§11) не работает." };
  }
  const age = status.latest_age_hours;
  if (age === null || age === undefined) {
    return { level: "warn", text: "Возраст последней копии неизвестен" };
  }
  if (age > STALE_HOURS) {
    const days = Math.floor(age / 24);
    return {
      level: "err",
      text: days >= 1
        ? `Последней копии ${days} дн. — автоматический бэкап не отработал`
        : `Последней копии ${age.toFixed(1)} ч — автоматический бэкап не отработал`,
    };
  }
  return { level: "ok", text: `Последняя копия ${age.toFixed(1)} ч назад` };
}
