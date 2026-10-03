export function Pager({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);
  return (
    <div className="row" style={{ alignItems: "center", marginTop: 12, gap: 8 }}>
      <button className="btn secondary" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Назад</button>
      <span className="muted">{from}–{to} из {total} · стр. {page} / {pages}</span>
      <button className="btn secondary" disabled={page >= pages} onClick={() => onPage(page + 1)}>Вперёд →</button>
    </div>
  );
}
