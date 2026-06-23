import { useState } from "react";
import { getToken } from "../api";

export function Reports() {
  const [days, setDays] = useState(30);
  const url = (name: string, ext: string) => `/api/reports/${name}.${ext}?days=${days}&token=${getToken()}`;

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
    </div>
  );
}
