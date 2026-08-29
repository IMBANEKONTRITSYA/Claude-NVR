/**
 * Сборка тела PUT /api/settings из состояния формы.
 *
 * Вынесено из обработчика `save()` (Settings.tsx) по двум причинам.
 * Во-первых, бэкенд объявляет схему с `extra="forbid"`: опечатка в имени
 * ключа или настройка, выпавшая из ТЗ, дают 422 на всю форму, а не молчаливый
 * пропуск одного поля — значит преобразование стоит проверять тестом, а не
 * глазами. Во-вторых, длинный литерал внутри try/catch делал обработчик
 * длиннее окна, которым backend/tests/test_ui_no_silent_failures.py ищет
 * необработанный отказ мутирующего вызова.
 *
 * Значения формы приходят строками (input value), поэтому каждое поле
 * приводится явно — с запасным значением на случай ключа, которого в БД
 * ещё нет (она развёрнута до появления настройки): `parseInt(undefined)`
 * даёт NaN, `JSON.stringify` превращает его в `null`, и бэкенд отвечает 422
 * на сохранение всей формы из-за поля, которого администратор не касался.
 */

/**
 * Число из строки формы, с запасным значением ТОЛЬКО на неразобранное.
 *
 * Привычное `parseInt(x) || fallback` здесь неверно: ноль — законное
 * значение как минимум у `frame_skip` («анализировать каждый кадр») и
 * `motion_prefilter` («префильтр выключен»), и оно молча подменялось бы
 * запасным. То есть администратор выключал бы префильтр, а сохранялся бы
 * включённый — без единого сообщения об ошибке.
 */
function num(raw: any, fallback: number, float = false): number {
  const v = float ? parseFloat(raw) : parseInt(raw, 10);
  return Number.isNaN(v) ? fallback : v;
}

export function settingsPayload(s: any): Record<string, unknown> {
  return {
    retention_days: num(s.retention_days, 30),
    motion_threshold: num(s.motion_threshold, 1500),
    similarity_threshold: num(s.similarity_threshold, 0.45, true),
    detection_fps: num(s.detection_fps, 5),
    event_cooldown_sec: num(s.event_cooldown_sec, 10),
    alert_cooldown_sec: num(s.alert_cooldown_sec, 300),
    telegram_bot_token: s.telegram_bot_token || "",
    telegram_chat_id: s.telegram_chat_id || "",
    // SPEC §11: почтовые уведомления
    smtp_host: s.smtp_host || "",
    smtp_port: num(s.smtp_port, 587),
    smtp_user: s.smtp_user || "",
    smtp_password: s.smtp_password || "",
    smtp_tls: s.smtp_tls || "starttls",
    smtp_from: s.smtp_from || "",
    alert_email_to: s.alert_email_to || "",
    alert_sound_enabled: num(s.alert_sound_enabled, 0),
    frame_skip: num(s.frame_skip, 1),
    motion_prefilter: num(s.motion_prefilter, 1),
    idle_fps: num(s.idle_fps, 2),
    face_model: s.face_model || "buffalo_s",
    upscale_mode: s.upscale_mode || "avatar",
    cluster_interval_min: num(s.cluster_interval_min, 15),
    detect_width: num(s.detect_width, 640),
    record_segment_min: num(s.record_segment_min, 5),
    analytics_cameras_max: num(s.analytics_cameras_max, 2),
    disk_min_free_pct: num(s.disk_min_free_pct, 5),
    disk_warn_pct: num(s.disk_warn_pct, 80),
    disk_crit_pct: num(s.disk_crit_pct, 90),
  };
}
