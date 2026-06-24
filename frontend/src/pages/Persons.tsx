import { useEffect, useRef, useState } from "react";
import { api, mediaUrl } from "../api";
import { useWebSocket } from "../useWebSocket";
import { Pager } from "../Pager";
import { useUI } from "../ui";

export function Persons() {
  const { toast } = useUI();
  const [persons, setPersons] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 48;
  const [filter, setFilter] = useState<string>("");
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<any | null>(null);
  const [gallery, setGallery] = useState<any[]>([]);
  const [mergeTarget, setMergeTarget] = useState<number | null>(null);
  const [enhMsg, setEnhMsg] = useState("");
  const selRef = useRef<any>(null);
  selRef.current = sel;

  const load = () => api.persons({ status: filter || undefined, q: q || undefined, page, page_size: PAGE_SIZE })
    .then((r: any) => { setPersons(r.items); setTotal(r.total); })
    .catch(() => {});
  useEffect(() => { load(); }, [filter, q, page]);
  useEffect(() => { setPage(1); }, [filter, q]);

  // Обновляем галерею/аватар при готовности апскейла
  useWebSocket("/ws/faces", (msg) => {
    if (msg.type !== "enhanced") return;
    if (selRef.current && msg.person_id === selRef.current.id) {
      api.personGallery(selRef.current.id).then(setGallery).catch(() => {});
    }
    load();
  });

  const open = async (p: any) => {
    setSel(p);
    setEnhMsg("");
    setGallery(await api.personGallery(p.id));
  };

  const rename = async () => {
    const name = prompt("Имя персоны:", sel.name);
    if (name === null) return;
    const u = await api.personUpdate(sel.id, { name });
    setSel(u);
    load();
  };

  const enhance = async () => {
    setEnhMsg("Поставлено в очередь...");
    try {
      const r = await api.personEnhance(sel.id);
      setEnhMsg(`В очереди на улучшение: ${r.queued} снимков`);
    } catch (e: any) { setEnhMsg(`Ошибка: ${e.message}`); }
  };

  const merge = async () => {
    if (!mergeTarget) return;
    await api.personMerge(sel.id, mergeTarget);
    setSel(null);
    setMergeTarget(null);
    load();
  };

  return (
    <div>
      <h2>Карточки персон</h2>
      <div className="toolbar">
        <select value={filter} onChange={e => setFilter(e.target.value)} style={{ width: 200 }}>
          <option value="">Все</option>
          <option value="known">Известные</option>
          <option value="unknown">Неизвестные</option>
        </select>
        <input placeholder="Поиск по имени" value={q} onChange={e => setQ(e.target.value)} style={{ width: 240 }} />
        <label className="btn" style={{ cursor: "pointer" }}>
          Создать персону
          <input type="file" accept="image/*" style={{ display: "none" }} onChange={async e => {
            const file = e.target.files?.[0]; if (!file) return;
            const name = prompt("Имя новой персоны:");
            if (!name) { e.target.value = ""; return; }
            try {
              const fd = new FormData();
              fd.append("name", name);
              fd.append("file", file);
              await api.personCreate(fd);
              load();
              toast("Персона создана", "ok");
            } catch (err: any) { toast(err.message, "err"); }
            finally { e.target.value = ""; }
          }} />
        </label>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div className="card">
          <h3>Список (всего: {total})</h3>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))" }}>
            {persons.map(p => (
              <div key={p.id} className={`tile ${p.status}`} style={{ cursor: "pointer", flexDirection: "column", textAlign: "center", position: "relative" }} onClick={() => open(p)}>
                {p.avatar_path ? <img src={mediaUrl(p.avatar_path)} /> : <div style={{ width: 64, height: 64, background: "#000" }} />}
                <div style={{ fontSize: 12 }}>{p.name || `Неизвестный #${p.id}`}</div>
                {p.alert_on_detection && <span title="В watchlist" style={{ position: "absolute", top: 4, right: 4, fontSize: 12 }}>⚠️</span>}
              </div>
            ))}
            {persons.length === 0 && <div className="empty">Пусто</div>}
          </div>
          <Pager page={page} pageSize={PAGE_SIZE} total={total} onPage={setPage} />
        </div>

        <div className="card">
          {sel ? (
            <>
              <h3>{sel.name || `Неизвестный #${sel.id}`}</h3>
              <div className="muted">ID {sel.id} · {sel.status}</div>
              <div className="row" style={{ marginTop: 8 }}>
                <button className="btn" onClick={rename}>Назначить имя</button>
                <button className="btn secondary" onClick={enhance}>Улучшить качество</button>
                <input type="number" placeholder="ID для слияния" value={mergeTarget ?? ""} onChange={e => setMergeTarget(parseInt(e.target.value) || null)} style={{ width: 160 }} />
                <button className="btn secondary" onClick={merge} disabled={!mergeTarget}>Слить</button>
              </div>
              {enhMsg && <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>{enhMsg}</div>}
              <div style={{ marginTop: 12 }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                  <input type="checkbox" style={{ width: "auto" }} checked={!!sel.alert_on_detection} onChange={async e => {
                    const u = await api.personUpdate(sel.id, { alert_on_detection: e.target.checked });
                    setSel({ ...sel, ...u });
                  }} />
                  В watchlist (Telegram-оповещение при детекции)
                </label>
                <label>Заметки</label>
                <textarea rows={3} defaultValue={sel.notes || ""} onBlur={async e => {
                  if (e.target.value !== (sel.notes || "")) {
                    const u = await api.personUpdate(sel.id, { notes: e.target.value });
                    setSel({ ...sel, ...u });
                  }
                }} />
              </div>
              <h4 style={{ marginTop: 16 }}>Галерея ({gallery.length})</h4>
              <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(90px, 1fr))" }}>
                {gallery.map(g => (
                  <div key={g.id} style={{ position: "relative" }}>
                    <img src={mediaUrl(g.snapshot_path)} style={{ width: "100%", aspectRatio: "1/1", objectFit: "cover", borderRadius: 4 }} />
                    {g.enhanced && <span style={{ position: "absolute", top: 2, right: 2, fontSize: 9, background: "var(--green)", color: "#000", padding: "0 4px", borderRadius: 3 }}>HD</span>}
                  </div>
                ))}
              </div>
            </>
          ) : <div className="empty">Выберите персону</div>}
        </div>
      </div>
    </div>
  );
}
