import { useState } from "react";
import { getToken } from "../api";

export function Reports() {
  const [days, setDays] = useState(7);
  const url = (ext: string) => `/api/reports/appearances.${ext}?days=${days}&token=${getToken()}`;

  return (
    <div>
      <h2>Отчёты</h2>
      <div className="card">
        <p>История появлений за период.</p>
        <div className="toolbar">
          <label style={{ margin: 0 }}>Дней:&nbsp;
            <input type="number" min={1} max={365} value={days} onChange={e => setDays(parseInt(e.target.value) || 7)} style={{ width: 100 }} />
          </label>
          <a className="btn" href={url("xlsx")}>Скачать Excel</a>
          <a className="btn secondary" href={url("csv")}>Скачать CSV</a>
        </div>
        <p className="muted">Примечание: текущая ссылка использует токен в query — оптимально для внутренней сети.</p>
      </div>
    </div>
  );
}
