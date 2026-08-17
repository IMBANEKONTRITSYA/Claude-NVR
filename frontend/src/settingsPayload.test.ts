import { describe, expect, it } from "vitest";
import { settingsPayload } from "./settingsPayload";

// Ключи, объявленные в SettingsUpdate на бэкенде (routers/settings.py).
// Схема там с extra="forbid", а поля необязательные — значит расхождение в
// ОБЕ стороны молчаливое: лишний ключ роняет сохранение всей формы в 422,
// пропущенный просто никогда не сохраняется. Список продублирован здесь
// намеренно: тест должен ловить расхождение, а не отражать его.
const BACKEND_KEYS = [
  "retention_days", "motion_threshold", "similarity_threshold", "detection_fps",
  "event_cooldown_sec", "alert_cooldown_sec", "telegram_bot_token",
  "telegram_chat_id", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
  "smtp_tls", "smtp_from", "alert_email_to", "alert_sound_enabled",
  "frame_skip", "motion_prefilter", "idle_fps", "face_model", "upscale_mode",
  "cluster_interval_min", "detect_width", "record_segment_min",
  "analytics_cameras_max", "disk_min_free_pct", "disk_warn_pct", "disk_crit_pct",
];

const FULL_FORM = {
  retention_days: "30", motion_threshold: "1500", similarity_threshold: "0.45",
  detection_fps: "5", event_cooldown_sec: "10", alert_cooldown_sec: "300",
  telegram_bot_token: "tok", telegram_chat_id: "chat",
  smtp_host: "smtp.object.local", smtp_port: "465", smtp_user: "nvr",
  smtp_password: "pass", smtp_tls: "ssl", smtp_from: "nvr@object.local",
  alert_email_to: "guard@object.local", alert_sound_enabled: "1",
  frame_skip: "1", motion_prefilter: "1", idle_fps: "2", face_model: "buffalo_s",
  upscale_mode: "avatar", cluster_interval_min: "15", detect_width: "640",
  record_segment_min: "5", analytics_cameras_max: "2", disk_min_free_pct: "5",
  disk_warn_pct: "80", disk_crit_pct: "90",
};

describe("тело PUT /api/settings", () => {
  it("шлёт ровно те ключи, которые принимает бэкенд", () => {
    expect(Object.keys(settingsPayload(FULL_FORM)).sort()).toEqual([...BACKEND_KEYS].sort());
  });

  it("приводит строки формы к числам", () => {
    const p = settingsPayload(FULL_FORM);
    expect(p.smtp_port).toBe(465);
    expect(p.retention_days).toBe(30);
    expect(p.similarity_threshold).toBe(0.45);
    expect(p.alert_sound_enabled).toBe(1);
  });

  it("не отдаёт NaN на настройке, которой нет в БД", () => {
    // БД, развёрнутая до появления SMTP-настроек, не содержит их ключей.
    // NaN уехал бы на бэкенд как null и уронил бы сохранение всей формы в
    // 422 — из-за поля, которого администратор не касался.
    const p = settingsPayload({});
    for (const [k, v] of Object.entries(p)) {
      expect(Number.isNaN(v as any), `${k} = NaN`).toBe(false);
    }
    expect(p.smtp_port).toBe(587);
    expect(p.smtp_tls).toBe("starttls");
    expect(p.alert_sound_enabled).toBe(0);
  });

  it("сохраняет ноль там, где ноль — законное значение", () => {
    // frame_skip = 0 значит «анализировать каждый кадр», motion_prefilter = 0
    // — «префильтр выключен». Привычное `parseInt(x) || fallback` подменило
    // бы их запасными значениями молча: администратор выключает префильтр, а
    // сохраняется включённый.
    const p = settingsPayload({ ...FULL_FORM, frame_skip: "0", motion_prefilter: "0" });
    expect(p.frame_skip).toBe(0);
    expect(p.motion_prefilter).toBe(0);
  });

  it("не теряет пустые строковые настройки", () => {
    // Пустой smtp_host — это «почту выключили». Если бы поле выпадало из
    // тела, выключить почту через форму было бы нельзя.
    const p = settingsPayload({ ...FULL_FORM, smtp_host: "", alert_email_to: "" });
    expect(p.smtp_host).toBe("");
    expect(p.alert_email_to).toBe("");
  });
});
