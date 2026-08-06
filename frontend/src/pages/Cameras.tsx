import { useEffect, useState } from "react";
import { api } from "../api";
import { useUI } from "../ui";

const EMPTY_FORM = {
  // Режим по умолчанию — только запись (SPEC §2): аналитика включается явно
  // на выбранных камерах, а не на каждой добавленной.
  name: "", rtsp_url: "", sub_rtsp_url: "", location: "", enabled: true, mode: "record_only",
  onvif_enabled: false, onvif_host: "", onvif_port: 80, onvif_username: "", onvif_password: "",
  // SPEC §5: собственная глубина хранения. Пустая строка — «следовать за
  // глобальной настройкой»; в payload уходит null, а не 0 (см. submit).
  retention_days: "",
};

export function Cameras() {
  const { toast, confirm } = useUI();
  const [cams, setCams] = useState<any[]>([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editing, setEditing] = useState<number | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string>("");
  const [discovering, setDiscovering] = useState(false);
  const [discovered, setDiscovered] = useState<any[] | null>(null);
  const [subnet, setSubnet] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkAdding, setBulkAdding] = useState(false);
  const [bulkResult, setBulkResult] = useState<any | null>(null);
  const [loadingProfiles, setLoadingProfiles] = useState(false);
  const [profiles, setProfiles] = useState<any[] | null>(null);

  const load = () => api.cameras().then(setCams).catch(() => {});
  useEffect(() => { load(); }, []);

  const submit = async () => {
    try {
      // Пустое поле глубины хранения — это null («следовать за глобальной»),
      // а не 0: бэкенд отвергает 0 (ge=1), и без приведения сохранение
      // камеры без собственного срока падало бы с 422.
      const payload = {
        ...form,
        retention_days: form.retention_days === "" ? null : Number(form.retention_days),
      };
      if (editing) await api.camUpdate(editing, payload);
      else await api.camAdd(payload);
      setForm(EMPTY_FORM);
      setEditing(null);
      load();
      toast(editing ? "Камера обновлена" : "Камера добавлена", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  const discoverOnvif = async () => {
    setDiscovering(true); setDiscovered(null);
    try {
      // Пустой диапазон = только WS-Discovery (multicast). Он не проходит
      // через NAT docker-сети, поэтому подсказка в поле объясняет, что при
      // пустом результате нужно указать подсеть.
      const r = await api.onvifDiscover(subnet.trim() || undefined);
      setDiscovered(r.devices || []);
      if (!r.devices?.length) {
        toast(
          subnet.trim()
            ? "Камеры в этом диапазоне не найдены"
            : "Камеры не найдены. Укажите диапазон подсети — multicast-поиск не проходит через сеть Docker",
          "err",
        );
      }
    } catch (e: any) { toast(e.message, "err"); }
    finally { setDiscovering(false); }
  };

  // ТЗ 18.7, вторая часть: "получение профилей потоков" — GetProfiles,
  // затем GetStreamUri по выбранному профилю автозаполняет RTSP URL формы
  // (аналогично тому, как автообнаружение уже автозаполняет host/port).
  const loadProfiles = async () => {
    setLoadingProfiles(true); setProfiles(null);
    try {
      const r = await api.onvifProfiles(form.onvif_host, form.onvif_port, form.onvif_username, form.onvif_password);
      setProfiles(r.profiles || []);
      if (!r.profiles?.length) toast("Профили потоков не найдены", "err");
    } catch (e: any) { toast(e.message, "err"); }
    finally { setLoadingProfiles(false); }
  };

  // Массовое добавление: имя, основной поток и субпоток каждой камеры
  // запрашиваются бэкендом у неё самой (OSD → ONVIF-скоуп → модель → IP),
  // поэтому здесь достаточно передать адрес и учётные данные.
  const bulkAdd = async () => {
    setBulkAdding(true); setBulkResult(null);
    try {
      const cameras = (discovered || [])
        .filter(d => selected.includes(d.host))
        .map(d => ({
          host: d.host,
          port: d.port || 80,
          username: form.onvif_username,
          password: form.onvif_password,
          scopes: d.scopes || [],
        }));
      const r = await api.onvifBulkAdd(cameras);
      setBulkResult(r);
      setSelected([]);
      load();
      if (r.added.length) toast(`Добавлено камер: ${r.added.length}`, "ok");
      else toast("Ни одной камеры добавить не удалось", "err");
    } catch (e: any) { toast(e.message, "err"); }
    finally { setBulkAdding(false); }
  };

  const pickProfile = async (token: string) => {
    try {
      const r = await api.onvifStreamUri(form.onvif_host, form.onvif_port, form.onvif_username, form.onvif_password, token);
      if (r.uri) { setForm({ ...form, rtsp_url: r.uri }); toast("RTSP URL заполнен из профиля", "ok"); }
    } catch (e: any) { toast(e.message, "err"); }
  };

  const remove = async (id: number) => {
    if (!(await confirm("Удалить камеру?"))) return;
    try {
      await api.camDelete(id);
      load();
      toast("Камера удалена", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  return (
    <div>
      <h2>Управление камерами</h2>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>{editing ? "Редактирование" : "Добавить камеру"}</h3>
        <div className="grid" style={{ gridTemplateColumns: "1fr 2fr 1fr", marginBottom: 8 }}>
          <div><label>Название</label><input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></div>
          <div><label>RTSP URL</label><input value={form.rtsp_url} onChange={e => setForm({ ...form, rtsp_url: e.target.value })} placeholder="rtsp://user:pass@ip:554/stream" /></div>
          <div><label>RTSP субпотока (для детекции)</label><input value={form.sub_rtsp_url} onChange={e => setForm({ ...form, sub_rtsp_url: e.target.value })} placeholder="640x360, необязательно" /></div>
          <div><label>Локация</label><input value={form.location} onChange={e => setForm({ ...form, location: e.target.value })} /></div>
        </div>
        <div style={{ marginBottom: 10, maxWidth: 460 }}>
          <label>Режим камеры</label>
          <select value={form.mode} onChange={e => setForm({ ...form, mode: e.target.value })}>
            <option value="record_only">Только запись — непрерывный архив, без распознавания</option>
            <option value="analytics">Аналитика — запись плюс детекция и распознавание лиц</option>
          </select>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            Запись ведётся в обоих режимах. Аналитика заметно нагружает процессор,
            поэтому её включают на нескольких выбранных камерах — предел задаётся
            в «Настройках» (по умолчанию 2).
          </div>
        </div>
        <div style={{ marginBottom: 10, maxWidth: 460 }}>
          <label>Глубина хранения этой камеры, суток</label>
          <input type="number" min={1} max={3650} value={form.retention_days}
            placeholder="как в общих настройках"
            onChange={e => setForm({ ...form, retention_days: e.target.value })} />
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            Пусто — камера следует за глобальной глубиной хранения из «Настроек»
            и продолжит следовать за ней при её изменении. Значение здесь
            переопределяет её только для этой камеры.
          </div>
        </div>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 12 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />
          Активна
        </label>
        <button className="btn" onClick={submit}>{editing ? "Сохранить" : "Добавить"}</button>
        <button className="btn secondary" style={{ marginLeft: 8 }} disabled={!form.rtsp_url || testing}
          onClick={async () => {
            setTesting(true); setTestResult("");
            try {
              const r = await api.testRtsp(form.rtsp_url);
              setTestResult(r.ok ? `OK — ${r.info?.split("\n")[0] || "поток доступен"}` : `Ошибка: ${r.error}`);
            } catch (e: any) { setTestResult(`Ошибка: ${e.message}`); }
            finally { setTesting(false); }
          }}>{testing ? "Проверка..." : "Проверить RTSP"}</button>
        {editing && <button className="btn secondary" onClick={() => { setEditing(null); setForm(EMPTY_FORM); setTestResult(""); }} style={{ marginLeft: 8 }}>Отмена</button>}
        {testResult && <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>{testResult}</div>}

        <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={form.onvif_enabled}
              onChange={e => setForm({ ...form, onvif_enabled: e.target.checked })} />
            ONVIF-события движения (вместо постоянного анализа кадров)
          </label>
          {form.onvif_enabled && (
            <>
              <div className="grid" style={{ gridTemplateColumns: "2fr 1fr 1fr 1fr" }}>
                <div><label>ONVIF-адрес камеры</label><input value={form.onvif_host}
                  onChange={e => setForm({ ...form, onvif_host: e.target.value })} placeholder="192.168.1.64" /></div>
                <div><label>Порт</label><input type="number" value={form.onvif_port}
                  onChange={e => setForm({ ...form, onvif_port: +e.target.value })} /></div>
                <div><label>Логин</label><input value={form.onvif_username}
                  onChange={e => setForm({ ...form, onvif_username: e.target.value })} /></div>
                <div><label>Пароль{editing ? " (оставить пустым — не менять)" : ""}</label>
                  <input type="password" value={form.onvif_password}
                    onChange={e => setForm({ ...form, onvif_password: e.target.value })} /></div>
              </div>
              <div style={{ marginTop: 8 }}>
                <label>Диапазон поиска (CIDR)</label>
                <input value={subnet} placeholder="192.168.1.0/24 — оставьте пустым для multicast-поиска"
                  onChange={e => setSubnet(e.target.value)} />
                <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                  Поиск через multicast (WS-Discovery) не проходит через сеть Docker,
                  поэтому обычно ничего не находит. Укажите подсеть, в которой стоят
                  камеры — например, если камера доступна по 192.168.105.19,
                  введите 192.168.105.0/24. Перебор до 1024 адресов, только приватные
                  диапазоны.
                </div>
              </div>
              <button type="button" className="btn secondary" style={{ marginTop: 8 }} disabled={discovering}
                onClick={discoverOnvif}>
                {discovering ? "Поиск..." : "Найти камеры в сети"}
              </button>
              {discovered && discovered.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 6 }}>
                    <strong>Найдено камер: {discovered.length}</strong>
                    <button type="button" className="btn secondary" style={{ padding: "2px 10px", fontSize: 12 }}
                      onClick={() => setSelected(
                        selected.length === discovered.length ? [] : discovered.map(d => d.host),
                      )}>
                      {selected.length === discovered.length ? "Снять все" : "Выбрать все"}
                    </button>
                    <span className="muted" style={{ fontSize: 12 }}>выбрано: {selected.length}</span>
                  </div>
                  <div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>
                    Отметьте камеры и нажмите «Добавить выбранные» — имя, основной поток
                    и субпоток подтянутся с каждой камеры автоматически. Логин и пароль
                    берутся из полей выше и должны подходить ко всем отмеченным.
                    Клик по адресу заполняет форму одной камерой.
                  </div>
                  {/* Сетка, а не список в столбик: в реальной сети находится
                      несколько десятков камер, и одна колонка на всю высоту
                      страницы нечитаема. Ограничение по высоте с прокруткой
                      не даёт списку вытеснить кнопку добавления за экран. */}
                  <div style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))",
                    gap: 6,
                    maxHeight: 320,
                    overflowY: "auto",
                    padding: 6,
                    border: "1px solid var(--border, #2a3142)",
                    borderRadius: 6,
                  }}>
                    {discovered.map((d, i) => {
                      const scope = d.scopes?.find((s: string) => s.includes("/name/"));
                      const label = scope ? decodeURIComponent(scope.split("/name/")[1]) : null;
                      const isSelected = selected.includes(d.host);
                      return (
                        <label key={i} title={label ? `${d.host} — ${label}` : d.host}
                          style={{
                            display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
                            padding: "5px 7px", borderRadius: 4, minWidth: 0,
                            background: isSelected ? "var(--accent-bg, #1e3a5f)" : "transparent",
                          }}>
                          <input type="checkbox" checked={isSelected}
                            onChange={e => setSelected(e.target.checked
                              ? [...selected, d.host]
                              : selected.filter(h => h !== d.host))} />
                          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
                            <span style={{ fontFamily: "monospace", fontSize: 12 }}>{d.host}</span>
                            {label && <span className="muted" style={{ fontSize: 11 }}> · {label}</span>}
                          </span>
                          <button type="button" className="btn secondary"
                            style={{ marginLeft: "auto", padding: "1px 7px", fontSize: 11, flexShrink: 0 }}
                            onClick={ev => {
                              ev.preventDefault();
                              setForm({ ...form, onvif_host: d.host, onvif_port: d.port || 80 });
                            }}>
                            в форму
                          </button>
                        </label>
                      );
                    })}
                  </div>
                  <button type="button" className="btn" style={{ marginTop: 8 }}
                    disabled={!selected.length || bulkAdding} onClick={bulkAdd}>
                    {bulkAdding ? "Добавление..." : `Добавить выбранные (${selected.length})`}
                  </button>
                  {bulkResult && (
                    <div style={{ marginTop: 8, fontSize: 12 }}>
                      {bulkResult.added.length > 0 && (
                        <div style={{ color: "var(--ok, #4ade80)" }}>
                          Добавлено: {bulkResult.added.map((a: any) =>
                            `${a.name}${a.has_substream ? "" : " (без субпотока)"}`).join(", ")}
                        </div>
                      )}
                      {bulkResult.skipped.length > 0 && (
                        <div className="muted">
                          Пропущено: {bulkResult.skipped.map((s: any) => `${s.host} — ${s.reason}`).join("; ")}
                        </div>
                      )}
                      {bulkResult.failed.length > 0 && (
                        <div style={{ color: "var(--err, #f87171)" }}>
                          Не удалось: {bulkResult.failed.map((f: any) => `${f.host} — ${f.error}`).join("; ")}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              <button type="button" className="btn secondary" style={{ marginTop: 8, marginLeft: 8 }}
                disabled={!form.onvif_host || loadingProfiles}
                onClick={loadProfiles}>
                {loadingProfiles ? "Запрос..." : "Получить профили потоков"}
              </button>
              {profiles && profiles.length > 0 && (
                <ul style={{ marginTop: 8, paddingLeft: 0, listStyle: "none" }}>
                  {profiles.map((p, i) => (
                    <li key={i} style={{ marginBottom: 4 }}>
                      <button type="button" className="btn secondary" onClick={() => pickProfile(p.token)}>
                        {p.name || p.token}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>

      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>Название</th><th>Локация</th><th>Режим</th><th>Статус</th><th>Активна</th><th></th></tr></thead>
          <tbody>
            {cams.map(c => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>
                  {c.name}
                  {c.has_substream && <span className="muted" style={{ fontSize: 10, marginLeft: 6 }} title="Детекция идёт по субпотоку">SUB</span>}
                  {c.onvif_enabled && <span className="muted" style={{ fontSize: 10, marginLeft: 6 }} title="Движение — по событиям ONVIF">ONVIF</span>}
                </td>
                <td>{c.location}</td>
                <td>{c.mode === "analytics" ? "Аналитика" : "Только запись"}</td>
                <td><span className={`badge ${c.status}`}>{c.status}</span></td>
                <td>
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
                    <input type="checkbox" style={{ width: "auto" }} checked={c.enabled} onChange={async e => {
                      try { await api.camToggle(c.id, e.target.checked); load(); toast(e.target.checked ? "Камера включена" : "Камера отключена", "ok"); }
                      catch (err: any) { toast(err.message, "err"); }
                    }} />
                    {c.enabled ? "да" : "нет"}
                  </label>
                </td>
                <td>
                  <button className="btn secondary" onClick={() => {
                    setEditing(c.id);
                    setForm({
                      name: c.name, rtsp_url: "", sub_rtsp_url: "", location: c.location, enabled: c.enabled,
                      mode: c.mode || "record_only",
                      retention_days: c.retention_days == null ? "" : String(c.retention_days),
                      onvif_enabled: !!c.onvif_enabled, onvif_host: "", onvif_port: 80, onvif_username: "", onvif_password: "",
                    });
                  }}>Изм.</button>
                  <button className="btn danger" onClick={() => remove(c.id)} style={{ marginLeft: 4 }}>Удалить</button>
                </td>
              </tr>
            ))}
            {cams.length === 0 && <tr><td colSpan={7} className="empty">Камер нет</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
