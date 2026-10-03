import { useEffect, useState } from "react";
import { api, getToken, getRole } from "../api";
import { useUI } from "../ui";
import { describeSchedule, emptySchedule } from "../reportSchedule";

export function Reports() {
  const { toast } = useUI();
  const [days, setDays] = useState(30);
  const url = (name: string, ext: string) => `/api/reports/${name}.${ext}?days=${days}&token=${getToken()}`;

  const isAdmin = getRole() === "admin";
  const [kinds, setKinds] = useState<{ key: string; title: string }[]>([]);
  const [schedules, setSchedules] = useState<any[]>([]);
  const [draft, setDraft] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.reportSchedules().then(setSchedules).catch(() => {});
  useEffect(() => {
    api.reportKinds().then((r: any) => setKinds(r.kinds || [])).catch(() => {});
    load();
  }, []);

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      const { id, last_sent_at, last_error, ...body } = draft;
      if (id) await api.updateReportSchedule(id, body);
      else await api.createReportSchedule(body);
      setDraft(null);
      await load();
      toast("Шаблон сохранён", "ok");
    } catch (e: any) { toast(e.message, "err"); }
    finally { setBusy(false); }
  };

  const remove = async (s: any) => {
    if (!confirm(`Удалить шаблон «${s.name}»?`)) return;
    try {
      await api.deleteReportSchedule(s.id);
      await load();
      toast("Шаблон удалён", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  const sendNow = async (s: any) => {
    try {
      await api.sendReportSchedule(s.id);
      await load();
      toast("Отчёт отправлен", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  const upd = (k: string, cast: (v: any) => any = (v) => v) => (e: any) =>
    setDraft((p: any) => ({ ...p, [k]: cast(e.target.value) }));

  const Block = ({ title, desc, name }: { title: string; desc: string; name: string }) => (
    <div className="card" style={{ marginBottom: 12 }}>
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      <p className="muted" style={{ marginTop: 0 }}>{desc}</p>
      <div className="row">
        <a className="btn" href={url(name, "xlsx")}>Excel</a>
        <a className="btn secondary" href={url(name, "csv")}>CSV</a>
      </div>
    </div>
  );

  return (
    <div>
      <h2>Отчёты</h2>
      <div className="toolbar">
        <label style={{ margin: 0 }}>Период (дней):&nbsp;
          <input type="number" min={1} max={365} value={days} onChange={e => setDays(parseInt(e.target.value) || 30)} style={{ width: 100 }} />
        </label>
      </div>
      <Block title="История появлений" desc="Все события обнаружения лиц: время, камера, персона." name="appearances" />
      <Block title="Сводка по персонам" desc="Число появлений, первое и последнее обнаружение по каждой персоне." name="persons" />
      <Block title="Активность по камерам" desc="Количество обнаружений и уникальных персон по каждой камере." name="cameras" />
      <p className="muted">Результаты поиска по фото экспортируются кнопкой «Экспорт CSV» на странице «Поиск по фото».</p>

      {/* SPEC §8: «Настраиваемые шаблоны отчётов» и «автоматическая
          отправка по расписанию (email)». Шаблон без расписания — просто
          сохранённый набор параметров с кнопкой «Отправить». */}
      <h3 style={{ marginTop: 28 }}>Шаблоны и автоматическая отправка</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Отчёт уходит на почту вложением. Параметры SMTP задаются в разделе «Настройки».
      </p>

      {schedules.length === 0 && <p className="muted">Шаблонов пока нет.</p>}
      {schedules.map(s => (
        <div className="card" key={s.id} style={{ marginBottom: 10 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <b>{s.name}</b>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {kinds.find(k => k.key === s.kind)?.title || s.kind} · {s.fmt.toUpperCase()} ·
                за {s.days} сут · {describeSchedule(s)}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                Получатели: {s.recipients || "как у алертов (см. Настройки)"}
              </div>
              {s.last_sent_at && (
                <div className="muted" style={{ fontSize: 12 }}>
                  Последняя отправка: {new Date(s.last_sent_at).toLocaleString()}
                </div>
              )}
              {/* Причина отказа обязана быть видна здесь: иначе «отчёт не
                  приходит» диагностируется только по логам сервера. */}
              {s.last_error && (
                <div style={{ fontSize: 12, color: "var(--red)" }}>Ошибка: {s.last_error}</div>
              )}
            </div>
            <div className="row">
              <button className="btn secondary" onClick={() => sendNow(s)}>Отправить</button>
              {isAdmin && <button className="btn secondary" onClick={() => setDraft({ ...s })}>Изм.</button>}
              {isAdmin && <button className="btn secondary" onClick={() => remove(s)}>Удалить</button>}
            </div>
          </div>
        </div>
      ))}

      {isAdmin && !draft && (
        <button className="btn" onClick={() => setDraft(emptySchedule())}>Добавить шаблон</button>
      )}

      {draft && (
        <div className="card" style={{ maxWidth: 560 }}>
          <h4 style={{ marginTop: 0 }}>{draft.id ? "Изменение шаблона" : "Новый шаблон"}</h4>
          <div style={{ marginBottom: 12 }}>
            <label>Название</label>
            <input value={draft.name} onChange={upd("name")} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label>Отчёт</label>
            <select value={draft.kind} onChange={upd("kind")}>
              {kinds.map(k => <option key={k.key} value={k.key}>{k.title}</option>)}
            </select>
          </div>
          <div style={{ marginBottom: 12 }}>
            <label>Формат</label>
            <select value={draft.fmt} onChange={upd("fmt")}>
              <option value="xlsx">Excel (.xlsx)</option>
              <option value="csv">CSV</option>
            </select>
          </div>
          <div style={{ marginBottom: 12 }}>
            <label>Период выборки (дней)</label>
            <input type="number" min={1} max={3650} value={draft.days}
                   onChange={upd("days", v => parseInt(v) || 7)} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label>Получатели</label>
            <input value={draft.recipients} onChange={upd("recipients")}
                   placeholder="через запятую; пусто — как у алертов" />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label>
              <input type="checkbox" style={{ width: "auto", marginRight: 8 }}
                     checked={!!draft.enabled}
                     onChange={e => setDraft((p: any) => ({ ...p, enabled: e.target.checked }))} />
              Отправлять автоматически
            </label>
          </div>
          {draft.enabled && (
            <>
              <div style={{ marginBottom: 12 }}>
                <label>Периодичность</label>
                <select value={draft.period} onChange={upd("period")}>
                  <option value="daily">Ежедневно</option>
                  <option value="weekly">Еженедельно</option>
                  <option value="monthly">Ежемесячно</option>
                </select>
              </div>
              {draft.period === "weekly" && (
                <div style={{ marginBottom: 12 }}>
                  <label>День недели</label>
                  <select value={draft.day_of_week} onChange={upd("day_of_week", v => parseInt(v))}>
                    {["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
                      .map((d, i) => <option key={i} value={i}>{d}</option>)}
                  </select>
                </div>
              )}
              {draft.period === "monthly" && (
                <div style={{ marginBottom: 12 }}>
                  <label>Число месяца</label>
                  {/* Максимум 28: расписание «31-го» молча не сработало бы
                      в феврале, и заметили бы это через месяцы. */}
                  <input type="number" min={1} max={28} value={draft.day_of_month}
                         onChange={upd("day_of_month", v => parseInt(v) || 1)} />
                  <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                    Не больше 28-го — чтобы отчёт приходил и в феврале
                  </div>
                </div>
              )}
              <div className="row" style={{ marginBottom: 12 }}>
                <label style={{ margin: 0 }}>Время:&nbsp;
                  <input type="number" min={0} max={23} style={{ width: 70 }}
                         value={draft.hour} onChange={upd("hour", v => parseInt(v) || 0)} />
                </label>
                <label style={{ margin: 0 }}>:&nbsp;
                  <input type="number" min={0} max={59} style={{ width: 70 }}
                         value={draft.minute} onChange={upd("minute", v => parseInt(v) || 0)} />
                </label>
                <span className="muted" style={{ fontSize: 11 }}>по времени сервера</span>
              </div>
            </>
          )}
          <div className="row">
            <button className="btn" onClick={save} disabled={busy}>
              {busy ? "Сохранение..." : "Сохранить"}
            </button>
            <button className="btn secondary" onClick={() => setDraft(null)}>Отмена</button>
          </div>
        </div>
      )}
    </div>
  );
}
