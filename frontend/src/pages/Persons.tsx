import { useEffect, useState } from "react";
import { api, mediaUrl } from "../api";

export function Persons() {
  const [persons, setPersons] = useState<any[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [sel, setSel] = useState<any | null>(null);
  const [gallery, setGallery] = useState<any[]>([]);
  const [mergeTarget, setMergeTarget] = useState<number | null>(null);

  const load = () => api.persons(filter || undefined).then(setPersons).catch(() => {});
  useEffect(() => { load(); }, [filter]);

  const open = async (p: any) => {
    setSel(p);
    setGallery(await api.personGallery(p.id));
  };

  const rename = async () => {
    const name = prompt("Имя персоны:", sel.name);
    if (name === null) return;
    const u = await api.personUpdate(sel.id, { name });
    setSel(u);
    load();
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
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div className="card">
          <h3>Список ({persons.length})</h3>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))" }}>
            {persons.map(p => (
              <div key={p.id} className={`tile ${p.status}`} style={{ cursor: "pointer", flexDirection: "column", textAlign: "center" }} onClick={() => open(p)}>
                {p.avatar_path ? <img src={mediaUrl(p.avatar_path)} /> : <div style={{ width: 64, height: 64, background: "#000" }} />}
                <div style={{ fontSize: 12 }}>{p.name || `Неизвестный #${p.id}`}</div>
              </div>
            ))}
            {persons.length === 0 && <div className="empty">Пусто</div>}
          </div>
        </div>

        <div className="card">
          {sel ? (
            <>
              <h3>{sel.name || `Неизвестный #${sel.id}`}</h3>
              <div className="muted">ID {sel.id} · {sel.status}</div>
              <div className="row" style={{ marginTop: 8 }}>
                <button className="btn" onClick={rename}>Назначить имя</button>
                <input type="number" placeholder="ID для слияния" value={mergeTarget ?? ""} onChange={e => setMergeTarget(parseInt(e.target.value) || null)} style={{ width: 160 }} />
                <button className="btn secondary" onClick={merge} disabled={!mergeTarget}>Слить</button>
              </div>
              <h4 style={{ marginTop: 16 }}>Галерея ({gallery.length})</h4>
              <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(90px, 1fr))" }}>
                {gallery.map(g => (
                  <img key={g.id} src={mediaUrl(g.snapshot_path)} style={{ width: "100%", aspectRatio: "1/1", objectFit: "cover", borderRadius: 4 }} />
                ))}
              </div>
            </>
          ) : <div className="empty">Выберите персону</div>}
        </div>
      </div>
    </div>
  );
}
