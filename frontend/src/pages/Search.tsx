import { useState } from "react";
import { api, mediaUrl, getToken } from "../api";
import { useUI } from "../ui";

export function Search() {
  const { toast } = useUI();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string>("");
  const [threshold, setThreshold] = useState(0.4);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [status, setStatus] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const onFile = (f: File | null) => {
    setFile(f);
    setPreview(f ? URL.createObjectURL(f) : "");
  };

  const run = async () => {
    if (!file) return;
    setBusy(true); setErr(""); setResults([]);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("threshold", String(threshold));
      if (dateFrom) fd.append("date_from", dateFrom);
      if (dateTo) fd.append("date_to", dateTo);
      if (status) fd.append("status", status);
      const r = await api.searchFace(fd);
      setResults(r);
      if (r.length === 0) setErr("Совпадений не найдено — попробуйте снизить порог схожести");
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const openSegment = (segId: number | null) => {
    if (!segId) { toast("Для этого появления нет видеофрагмента", "warn"); return; }
    window.open(`/api/archive/file/${segId}?token=${getToken()}`, "_blank");
  };

  const exportCsv = () => {
    const head = ["event_id", "Имя", "Статус", "Камера", "Время", "Схожесть"];
    const rows = results.map(r => [r.event_id, r.name, r.status || "", r.camera_id, r.ts, r.similarity]);
    const csv = [head, ...rows].map(row => row.map(c => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "face_search.csv";
    a.click();
  };

  return (
    <div>
      <h2>Поиск похожих лиц</h2>
      <div className="grid" style={{ gridTemplateColumns: "320px 1fr", gap: 16 }}>
        <div className="card">
          <h3>Запрос</h3>
          <input type="file" accept="image/*" onChange={e => onFile(e.target.files?.[0] || null)} />
          {preview && <img src={preview} style={{ width: "100%", marginTop: 10, borderRadius: 6, maxHeight: 220, objectFit: "contain", background: "#000" }} />}
          <div style={{ marginTop: 12 }}>
            <label>Порог схожести: {threshold.toFixed(2)}</label>
            <input type="range" min={0.1} max={0.9} step={0.05} value={threshold} onChange={e => setThreshold(+e.target.value)} />
          </div>
          <div style={{ marginTop: 8 }}>
            <label>С даты</label>
            <input type="datetime-local" value={dateFrom} onChange={e => setDateFrom(e.target.value)} />
          </div>
          <div style={{ marginTop: 8 }}>
            <label>По дату</label>
            <input type="datetime-local" value={dateTo} onChange={e => setDateTo(e.target.value)} />
          </div>
          <div style={{ marginTop: 8 }}>
            <label>Статус</label>
            <select value={status} onChange={e => setStatus(e.target.value)}>
              <option value="">Любой</option>
              <option value="known">Известные</option>
              <option value="unknown">Неизвестные</option>
            </select>
          </div>
          <button className="btn" style={{ marginTop: 12, width: "100%" }} disabled={!file || busy} onClick={run}>
            {busy ? "Поиск..." : "Найти"}
          </button>
          {err && <div className="muted" style={{ marginTop: 8, color: "var(--orange)" }}>{err}</div>}
        </div>

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h3 style={{ margin: 0 }}>Результаты: {results.length}</h3>
            {results.length > 0 && <button className="btn secondary" onClick={exportCsv}>Экспорт CSV</button>}
          </div>
          <table style={{ marginTop: 10 }}>
            <thead><tr><th>Фото</th><th>Имя</th><th>Статус</th><th>Камера</th><th>Время</th><th>Схожесть</th><th></th></tr></thead>
            <tbody>
              {results.map(r => (
                <tr key={r.event_id}>
                  <td>{r.snapshot_path ? <img src={mediaUrl(r.snapshot_path)} style={{ width: 40, height: 40, objectFit: "cover", borderRadius: 4 }} /> : "—"}</td>
                  <td>{r.name}</td>
                  <td>{r.status || "—"}</td>
                  <td>#{r.camera_id}</td>
                  <td>{r.ts ? new Date(r.ts).toLocaleString("ru-RU") : "—"}</td>
                  <td><b style={{ color: r.similarity >= 0.6 ? "var(--green)" : "var(--orange)" }}>{(r.similarity * 100).toFixed(1)}%</b></td>
                  <td><button className="btn secondary" onClick={() => openSegment(r.segment_id)}>В архив</button></td>
                </tr>
              ))}
              {results.length === 0 && <tr><td colSpan={7} className="empty">Загрузите фото и нажмите «Найти»</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
