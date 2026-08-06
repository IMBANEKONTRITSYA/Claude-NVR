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
  const [tab, setTab] = useState<"form" | "scan">("form");
  const [filter, setFilter] = useState("");
  // Учётные данные для поиска в сети — свои, а не из формы камеры. Раньше
  // массовое добавление брало логин и пароль из полей редактируемой камеры,
  // и чтобы найти камеры в сети, приходилось сперва включить на ней галку
  // «ONVIF-события движения» — настройку, к поиску отношения не имеющую.
  const [scanUser, setScanUser] = useState("");
  const [scanPass, setScanPass] = useState("");

  const load = () => api.cameras().then((list: any[]) => {
    setCams(list);
    // Камера, которую правили, исчезла (удалена здесь или в другой вкладке)
    // — выходим из режима редактирования. Иначе форма продолжала бы слать
    // PUT на несуществующий id и отвечать «Камера не найдена» на каждое
    // сохранение, а список при этом стоял бы пустой.
    setEditing(prev => {
      if (prev !== null && !list.some(c => c.id === prev)) {
        setForm(EMPTY_FORM);
        return null;
      }
      return prev;
    });
  }).catch(() => {});
  useEffect(() => { load(); }, []);

  /** Открывает камеру в форме, подтянув её RTSP-адрес (он не приходит в списке). */
  const startEdit = async (c: any) => {
    setEditing(c.id);
    setTestResult("");
    const base = {
      name: c.name, rtsp_url: "", sub_rtsp_url: "", location: c.location, enabled: c.enabled,
      mode: c.mode || "record_only",
      retention_days: c.retention_days == null ? "" : String(c.retention_days),
      onvif_enabled: !!c.onvif_enabled, onvif_host: c.onvif_host || "",
      onvif_port: c.onvif_port || 80, onvif_username: c.onvif_username || "",
      onvif_password: "",
    };
    setForm(base);
    try {
      const { rtsp_url } = await api.camRtsp(c.id);
      setForm(f => ({ ...f, rtsp_url }));
    } catch (e: any) {
      // Адрес не отдался — честно говорим об этом, а не оставляем пустое
      // поле молча: сохранение с пустым адресом отвергнет валидатор, и
      // причина была бы неочевидна.
      toast(`Не удалось получить RTSP-адрес камеры: ${e.message}`, "err");
    }
  };

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
    } catch (e: any) {
      toast(e.message, "err");
      // «Камера не найдена» на PUT значит, что правившаяся камера исчезла.
      // Форма обязана выйти из режима редактирования, иначе каждое
      // следующее сохранение упирается в тот же 404 без единой подсказки,
      // что делать.
      if (editing && /не найдена/i.test(e.message || "")) {
        setEditing(null);
        setForm(EMPTY_FORM);
        load();
      }
    }
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
          username: scanUser,
          password: scanPass,
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

  const shown = filter.trim()
    ? cams.filter(c => `${c.id} ${c.name} ${c.location}`.toLowerCase().includes(filter.trim().toLowerCase()))
    : cams;

  return (
    <div>
      <h2>Управление камерами</h2>

      {/* Две независимые задачи — завести камеру вручную и найти камеры в
          сети — разведены по вкладкам. Подряд на одной странице они дают
          скролл, в котором список камер уходит за экран. */}
      <div className="tabs">
        <button className={tab === "form" ? "active" : ""} onClick={() => setTab("form")}>
          {editing ? `Редактирование: ${form.name || "камера"}` : "Добавить камеру"}
        </button>
        <button className={tab === "scan" ? "active" : ""} onClick={() => setTab("scan")}>
          Поиск камер в сети (ONVIF)
        </button>
      </div>

      {tab === "form" && (
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 24, alignItems: "start" }}>
          <div>
            <div className="section">
              <h4>Основное</h4>
              <div className="field">
                <label>Название</label>
                <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
              </div>
              <div className="field">
                <label>Локация</label>
                <input value={form.location} onChange={e => setForm({ ...form, location: e.target.value })} />
              </div>
              <div className="field">
                <label>Режим камеры</label>
                <select value={form.mode} onChange={e => setForm({ ...form, mode: e.target.value })}>
                  <option value="record_only">Только запись — непрерывный архив, без распознавания</option>
                  <option value="analytics">Аналитика — запись плюс детекция и распознавание лиц</option>
                </select>
                <div className="hint">
                  Запись ведётся в обоих режимах. Аналитика заметно нагружает процессор,
                  поэтому её включают на нескольких выбранных камерах — предел задаётся
                  в «Настройках» (по умолчанию 2).
                </div>
              </div>
              <div className="field">
                <label>Глубина хранения этой камеры, суток</label>
                <input type="number" min={1} max={3650} value={form.retention_days}
                  placeholder="как в общих настройках"
                  onChange={e => setForm({ ...form, retention_days: e.target.value })} />
                <div className="hint">
                  Пусто — камера следует за глобальной глубиной хранения из «Настроек»
                  и продолжит следовать за ней при её изменении.
                </div>
              </div>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input type="checkbox" style={{ width: "auto" }} checked={form.enabled}
                  onChange={e => setForm({ ...form, enabled: e.target.checked })} />
                Активна
              </label>
            </div>
          </div>

          <div>
            <div className="section">
              <h4>Потоки</h4>
              <div className="field">
                <label>RTSP основного потока — запись и просмотр</label>
                <input value={form.rtsp_url} onChange={e => setForm({ ...form, rtsp_url: e.target.value })}
                  placeholder="rtsp://user:pass@ip:554/stream" />
              </div>
              <div className="field">
                <label>RTSP субпотока — только для детекции</label>
                <input value={form.sub_rtsp_url} onChange={e => setForm({ ...form, sub_rtsp_url: e.target.value })}
                  placeholder="640x360, необязательно" />
                <div className="hint">
                  В архив пишется всегда основной поток. Субпоток используется
                  только слоем аналитики и только если он не ниже 640×480.
                </div>
              </div>
              <button className="btn secondary sm" disabled={!form.rtsp_url || testing}
                onClick={async () => {
                  setTesting(true); setTestResult("");
                  try {
                    const r = await api.testRtsp(form.rtsp_url);
                    setTestResult(r.ok ? `OK — ${r.info?.split("\n")[0] || "поток доступен"}` : `Ошибка: ${r.error}`);
                  } catch (e: any) { setTestResult(`Ошибка: ${e.message}`); }
                  finally { setTesting(false); }
                }}>{testing ? "Проверка..." : "Проверить RTSP"}</button>
              {testResult && <div className="hint" style={{ marginTop: 6 }}>{testResult}</div>}
            </div>

            <div className="section">
              <h4>ONVIF</h4>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 10 }}>
                <input type="checkbox" style={{ width: "auto" }} checked={form.onvif_enabled}
                  onChange={e => setForm({ ...form, onvif_enabled: e.target.checked })} />
                События движения от камеры (вместо анализа кадров)
              </label>
              {form.onvif_enabled && (
                <>
                  <div className="grid" style={{ gridTemplateColumns: "2fr 1fr" }}>
                    <div className="field"><label>Адрес камеры</label>
                      <input value={form.onvif_host} placeholder="192.168.1.64"
                        onChange={e => setForm({ ...form, onvif_host: e.target.value })} /></div>
                    <div className="field"><label>Порт</label>
                      <input type="number" value={form.onvif_port}
                        onChange={e => setForm({ ...form, onvif_port: +e.target.value })} /></div>
                    <div className="field"><label>Логин</label>
                      <input value={form.onvif_username}
                        onChange={e => setForm({ ...form, onvif_username: e.target.value })} /></div>
                    <div className="field"><label>Пароль{editing ? " (пусто — не менять)" : ""}</label>
                      <input type="password" value={form.onvif_password}
                        onChange={e => setForm({ ...form, onvif_password: e.target.value })} /></div>
                  </div>
                  <button type="button" className="btn secondary sm"
                    disabled={!form.onvif_host || loadingProfiles} onClick={loadProfiles}>
                    {loadingProfiles ? "Запрос..." : "Получить профили потоков"}
                  </button>
                  {profiles && profiles.length > 0 && (
                    <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {profiles.map((p, i) => (
                        <button key={i} type="button" className="btn secondary sm"
                          onClick={() => pickProfile(p.token)}>{p.name || p.token}</button>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </div>

        <div className="toolbar" style={{ marginTop: 16, marginBottom: 0, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
          <button className="btn" onClick={submit}>{editing ? "Сохранить" : "Добавить камеру"}</button>
          {editing && (
            <button className="btn secondary"
              onClick={() => { setEditing(null); setForm(EMPTY_FORM); setTestResult(""); }}>
              Отмена
            </button>
          )}
        </div>
      </div>
      )}

      {tab === "scan" && (
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="section">
          <h4>Где и под какой учётной записью искать</h4>
          <div className="grid" style={{ gridTemplateColumns: "2fr 1fr 1fr" }}>
            <div className="field">
              <label>Диапазон поиска (CIDR)</label>
              <input value={subnet} placeholder="192.168.105.0/24"
                onChange={e => setSubnet(e.target.value)} />
            </div>
            <div className="field"><label>Логин камер</label>
              <input value={scanUser} onChange={e => setScanUser(e.target.value)} /></div>
            <div className="field"><label>Пароль камер</label>
              <input type="password" value={scanPass} onChange={e => setScanPass(e.target.value)} /></div>
          </div>
          <div className="hint" style={{ marginBottom: 10 }}>
            Multicast-поиск (WS-Discovery) не проходит через сеть Docker и обычно
            ничего не находит — укажите подсеть, в которой стоят камеры. Например,
            если камера доступна по 192.168.105.19, введите 192.168.105.0/24.
            Перебор до 1024 адресов, только приватные диапазоны. Логин и пароль
            должны подходить ко всем отмеченным камерам.
          </div>
          <button type="button" className="btn" disabled={discovering} onClick={discoverOnvif}>
            {discovering ? "Поиск..." : "Найти камеры в сети"}
          </button>
        </div>

        {discovered && discovered.length > 0 && (
          <div className="section">
            <h4>Найдено камер: {discovered.length}</h4>
            <div className="toolbar">
              <button type="button" className="btn secondary sm"
                onClick={() => setSelected(
                  selected.length === discovered.length ? [] : discovered.map(d => d.host))}>
                {selected.length === discovered.length ? "Снять все" : "Выбрать все"}
              </button>
              <span className="muted" style={{ fontSize: 12 }}>выбрано: {selected.length}</span>
              <button type="button" className="btn" style={{ marginLeft: "auto" }}
                disabled={!selected.length || bulkAdding} onClick={bulkAdd}>
                {bulkAdding ? "Добавление..." : `Добавить выбранные (${selected.length})`}
              </button>
            </div>
            <div className="hint" style={{ marginBottom: 8 }}>
              Имя, основной поток и субпоток подтянутся с каждой камеры автоматически.
              «В форму» переносит один адрес в карточку камеры для ручной настройки.
            </div>
            {/* Сетка, а не столбик: в реальной сети находятся десятки камер.
                Ограничение по высоте с прокруткой не даёт списку вытеснить
                кнопку добавления за экран. */}
            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(310px, 1fr))",
              gap: 6, maxHeight: 320, overflowY: "auto", padding: 6,
              border: "1px solid var(--border)", borderRadius: 6,
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
                      background: isSelected ? "#1e3a5f" : "transparent",
                    }}>
                    <input type="checkbox" checked={isSelected} style={{ width: "auto" }}
                      onChange={e => setSelected(e.target.checked
                        ? [...selected, d.host]
                        : selected.filter(h => h !== d.host))} />
                    {/* Адрес и имя — разные элементы, а не один обрезаемый:
                        пока они лежали в общем span с ellipsis, длинное имя
                        камеры съедало сам IP и в списке оставалось «192.1…». */}
                    <span style={{ fontFamily: "monospace", fontSize: 12, flexShrink: 0 }}>{d.host}</span>
                    {label && (
                      <span className="muted" style={{
                        fontSize: 11, minWidth: 0,
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}>{label}</span>
                    )}
                    <button type="button" className="btn secondary"
                      style={{ marginLeft: "auto", padding: "1px 7px", fontSize: 11, flexShrink: 0 }}
                      onClick={ev => {
                        ev.preventDefault();
                        setForm({ ...form, onvif_enabled: true, onvif_host: d.host, onvif_port: d.port || 80,
                                  onvif_username: scanUser, onvif_password: scanPass });
                        setTab("form");
                      }}>
                      в форму
                    </button>
                  </label>
                );
              })}
            </div>
            {bulkResult && (
              <div style={{ marginTop: 10, fontSize: 12 }}>
                {bulkResult.added.length > 0 && (
                  <div style={{ color: "var(--green)" }}>
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
                  <div style={{ color: "var(--red)" }}>
                    Не удалось: {bulkResult.failed.map((f: any) => `${f.host} — ${f.error}`).join("; ")}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
      )}

      <div className="card">
        <div className="toolbar">
          <strong>Камеры: {cams.length}</strong>
          <span className="muted" style={{ fontSize: 12 }}>
            в сети {cams.filter(c => c.status === "online").length},
            аналитика {cams.filter(c => c.mode === "analytics").length}
          </span>
          {/* Фильтр, а не пагинация: на 120 камерах нужную ищут по имени
              или локации, а не листают страницы. */}
          <input value={filter} onChange={e => setFilter(e.target.value)}
            placeholder="Фильтр по имени, локации или ID"
            style={{ marginLeft: "auto", maxWidth: 280 }} />
        </div>
        <div className="table-scroll">
          <table>
            <thead><tr>
              <th style={{ width: 56 }}>ID</th><th>Название</th><th>Локация</th>
              <th style={{ width: 130 }}>Режим</th><th style={{ width: 90 }}>Статус</th>
              <th style={{ width: 90 }}>Активна</th><th style={{ width: 150 }}></th>
            </tr></thead>
            <tbody>
              {shown.map(c => (
                <tr key={c.id}>
                  <td className="muted">{c.id}</td>
                  <td>
                    {c.name}
                    {c.has_substream && <span className="muted" style={{ fontSize: 10, marginLeft: 6 }} title="У камеры есть субпоток для детекции">SUB</span>}
                    {c.onvif_enabled && <span className="muted" style={{ fontSize: 10, marginLeft: 6 }} title="Движение — по событиям ONVIF">ONVIF</span>}
                  </td>
                  <td className="muted">{c.location || "—"}</td>
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
                    <button className="btn secondary sm"
                      onClick={() => { startEdit(c); setTab("form"); }}>Изм.</button>
                    <button className="btn danger sm" onClick={() => remove(c.id)} style={{ marginLeft: 4 }}>Удалить</button>
                  </td>
                </tr>
              ))}
              {shown.length === 0 && (
                <tr><td colSpan={7} className="empty">
                  {cams.length ? "Ничего не найдено по фильтру" : "Камер нет"}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
