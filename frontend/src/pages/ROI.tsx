import { useEffect, useRef, useState } from "react";
import { api, camSnapshotUrl } from "../api";
import { useUI } from "../ui";

export function ROI() {
  const { toast } = useUI();
  const [cams, setCams] = useState<any[]>([]);
  const [camId, setCamId] = useState<number | null>(null);
  const [poly, setPoly] = useState<number[][]>([]);
  const [polys, setPolys] = useState<number[][][]>([]);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const W = 800, H = 450;

  // SPEC §11: «доступно только для камер в режиме analytics». Бэкенд это и
  // проверяет (PUT /roi отдаёт 400 для record_only), но выпадающий список
  // отдавал ВСЕ камеры — а record_only по §3 режим по умолчанию, то есть на
  // объекте из 120 камер 118 позиций списка приводили к отказу уже после
  // того, как оператор нарисовал полигон.
  useEffect(() => {
    api.cameras()
      .then((all: any[]) => setCams(all.filter(c => c.mode === "analytics")))
      .catch((e: any) => toast(e.message, "err"));
  }, []);
  useEffect(() => {
    if (camId == null) return;
    api.camRoiGet(camId).then(r => {
      const ps = (r.polygons || []) as number[][][];
      setPolys(ps.map(p => p.map(([x, y]) => [x * W, y * H])));
    });
    setPoly([]);
  }, [camId]);

  const [snapKey, setSnapKey] = useState(0);
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext("2d")!;
    const draw = (p: number[][], color: string) => {
      if (p.length === 0) return;
      ctx.strokeStyle = color; ctx.fillStyle = color + "33"; ctx.lineWidth = 2;
      ctx.beginPath();
      p.forEach((pt, i) => i === 0 ? ctx.moveTo(pt[0], pt[1]) : ctx.lineTo(pt[0], pt[1]));
      ctx.closePath(); ctx.fill(); ctx.stroke();
    };
    const render = (bg?: HTMLImageElement) => {
      if (bg) ctx.drawImage(bg, 0, 0, W, H);
      else { ctx.fillStyle = "#222"; ctx.fillRect(0, 0, W, H); ctx.fillStyle = "#666"; ctx.font = "14px sans-serif"; ctx.fillText("Кадр недоступен — выберите камеру", 16, 24); }
      polys.forEach(p => draw(p, "#3fb950"));
      draw(poly, "#2f81f7");
    };
    if (camId == null) { render(); return; }
    const img = new Image();
    img.onload = () => render(img);
    img.onerror = () => render();
    img.src = camSnapshotUrl(camId);
  }, [poly, polys, camId, snapKey]);

  useEffect(() => {
    if (camId == null) return;
    const t = setInterval(() => setSnapKey(k => k + 1), 5000);
    return () => clearInterval(t);
  }, [camId]);

  const click = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const r = canvasRef.current!.getBoundingClientRect();
    setPoly(p => [...p, [e.clientX - r.left, e.clientY - r.top]]);
  };

  const closePoly = () => {
    if (poly.length < 3) return;
    setPolys([...polys, poly]);
    setPoly([]);
  };

  const save = async () => {
    if (camId == null) return;
    const norm = polys.map(p => p.map(([x, y]) => [x / W, y / H]));
    // Без catch отказ сохранения был не виден вовсе: req() бросает Error на
    // любой !res.ok, toast об успехе просто не выполнялся, и оператор
    // оставался с нарисованным полигоном и без единого сообщения.
    try {
      await api.camRoiPut(camId, norm);
      toast("Зоны сохранены", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  return (
    <div>
      <h2>Зоны детекции</h2>
      <div className="toolbar">
        <select value={camId ?? ""} onChange={e => setCamId(parseInt(e.target.value) || null)}>
          <option value="">— камера —</option>
          {cams.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        {cams.length === 0 && (
          <span className="muted">
            Нет камер в режиме аналитики — зоны детекции задаются только для них.
            Переведите камеру в режим «аналитика» на странице «Камеры».
          </span>
        )}
        <button className="btn secondary" onClick={closePoly} disabled={poly.length < 3}>Закрыть полигон</button>
        <button className="btn secondary" onClick={() => setPoly([])}>Сбросить текущий</button>
        <button className="btn secondary" onClick={() => setPolys([])}>Очистить все</button>
        <button className="btn" onClick={save} disabled={camId == null}>Сохранить</button>
      </div>
      <canvas ref={canvasRef} width={W} height={H} style={{ border: "1px solid var(--border)", cursor: "crosshair" }} onClick={click} />
      <p className="muted">Кликайте по кадру для рисования полигона. Детекция будет выполняться только внутри зон.</p>
    </div>
  );
}
