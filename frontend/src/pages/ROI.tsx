import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export function ROI() {
  const [cams, setCams] = useState<any[]>([]);
  const [camId, setCamId] = useState<number | null>(null);
  const [poly, setPoly] = useState<number[][]>([]);
  const [polys, setPolys] = useState<number[][][]>([]);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const W = 800, H = 450;

  useEffect(() => { api.cameras().then(setCams); }, []);
  useEffect(() => {
    if (camId == null) return;
    api.camRoiGet(camId).then(r => setPolys(r.polygons || []));
    setPoly([]);
  }, [camId]);

  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext("2d")!;
    ctx.fillStyle = "#222"; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#666"; ctx.font = "14px sans-serif";
    ctx.fillText("Стоп-кадр камеры (заглушка)", 16, 24);
    const draw = (p: number[][], color: string) => {
      if (p.length === 0) return;
      ctx.strokeStyle = color; ctx.fillStyle = color + "33"; ctx.lineWidth = 2;
      ctx.beginPath();
      p.forEach((pt, i) => i === 0 ? ctx.moveTo(pt[0], pt[1]) : ctx.lineTo(pt[0], pt[1]));
      ctx.closePath(); ctx.fill(); ctx.stroke();
    };
    polys.forEach(p => draw(p, "#3fb950"));
    draw(poly, "#2f81f7");
  }, [poly, polys]);

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
    await api.camRoiPut(camId, polys);
    alert("Зоны сохранены");
  };

  return (
    <div>
      <h2>Зоны детекции</h2>
      <div className="toolbar">
        <select value={camId ?? ""} onChange={e => setCamId(parseInt(e.target.value) || null)}>
          <option value="">— камера —</option>
          {cams.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
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
