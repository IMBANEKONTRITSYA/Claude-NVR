import { useEffect, useRef, useState } from "react";
import { api, mediaUrl } from "../api";
import { useWebSocket } from "../useWebSocket";
import { Pager } from "../Pager";
import { useUI } from "../ui";
import { addTag, removeTag } from "../personTags";

export function Persons() {
  const { toast, confirm } = useUI();
  const [persons, setPersons] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 48;
  const [filter, setFilter] = useState<string>("");
  const [q, setQ] = useState("");
  // SPEC §15: фильтр по тегу и справочник тегов для выпадающего списка.
  const [tag, setTag] = useState<string>("");
  const [tagCatalog, setTagCatalog] = useState<{ tag: string; count: number }[]>([]);
  const [tagDraft, setTagDraft] = useState("");
  const [sel, setSel] = useState<any | null>(null);
  const [gallery, setGallery] = useState<any[]>([]);
  const [mergeTarget, setMergeTarget] = useState<number | null>(null);
  const [enhMsg, setEnhMsg] = useState("");
  const selRef = useRef<any>(null);
  selRef.current = sel;

  const load = () => api.persons({ status: filter || undefined, q: q || undefined, tag: tag || undefined, page, page_size: PAGE_SIZE })
    .then((r: any) => { setPersons(r.items); setTotal(r.total); })
    .catch(() => {});
  // Справочник перечитывается после каждой правки тегов: иначе только что
  // заведённого тега нет в фильтре до перезагрузки страницы.
  const loadTags = () => api.personTags()
    .then((r: any) => setTagCatalog(Array.isArray(r) ? r : []))
    .catch(() => {});
  useEffect(() => { load(); }, [page]);
  useEffect(() => { loadTags(); }, []);
  useEffect(() => {
    if (page !== 1) setPage(1);
    else load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, q, tag]);

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
    try {
      const u = await api.personUpdate(sel.id, { name });
      setSel(u);
      load();
    } catch (e: any) { toast(e.message, "err"); }
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
    try {
      await api.personMerge(sel.id, mergeTarget);
      setSel(null);
      setMergeTarget(null);
      load();
    } catch (e: any) { toast(e.message, "err"); }
  };

  // Сохранение тегов идёт целиком списком: PATCH принимает `tags` как
  // полное новое состояние, поэтому add/remove считаются на клиенте, а
  // сервер получает результат — без частичных операций и гонок между
  // двумя вкладками, открытыми на одной карточке.
  const saveTags = async (next: string[]) => {
    try {
      const u = await api.personUpdate(sel.id, { tags: next });
      setSel({ ...sel, ...u });
      setTagDraft("");
      load();
      loadTags();
    } catch (err: any) { toast(err.message, "err"); }
  };

  const onAddTag = async () => {
    const r = addTag(sel.tags || [], tagDraft);
    if (!r.ok) { toast(r.error, "err"); return; }
    await saveTags(r.tags);
  };

  const label = () => sel.name || `Неизвестный #${sel.id}`;

  // Текст диалога раньше обещал «и все её снимки», а бэкенд сносил только
  // карточку: снимки и эмбеддинги оставались в базе и находились поиском по
  // фото. Обещание и действие разведены на две кнопки, и каждая говорит
  // ровно то, что делает.
  const remove = async () => {
    if (!(await confirm(
      `Удалить карточку «${label()}»? Снимки и события останутся в архиве без владельца. ` +
      `Чтобы стереть и их, используйте «Удалить с биометрией».`
    ))) return;
    try {
      await api.personDelete(sel.id);
      setSel(null);
      load();
      toast("Карточка удалена, архив не тронут", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  // SPEC §24: удаление биометрии по требованию. Необратимо и затрагивает
  // архив, поэтому подтверждение называет число кадров, которые исчезнут, —
  // оно же и подтверждает исполнение требования в ответе.
  const erase = async () => {
    if (!(await confirm(
      `Стереть биометрию «${label()}» (SPEC §24)? Будут удалены карточка, ` +
      `${gallery.length ? `все её кадры (в галерее ${gallery.length})` : "все её кадры"}, ` +
      `эмбеддинги и файлы снимков. Действие необратимо.`
    ))) return;
    try {
      const r: any = await api.personEraseBiometrics(sel.id);
      setSel(null);
      load();
      toast(`Биометрия удалена: событий ${r.events_removed}, файлов ${r.files_removed}`, "ok");
    } catch (e: any) { toast(e.message, "err"); }
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
        <select value={tag} onChange={e => setTag(e.target.value)} style={{ width: 220 }} aria-label="Фильтр по тегу">
          <option value="">Все теги</option>
          {tagCatalog.map(t => (
            <option key={t.tag} value={t.tag}>{t.tag} ({t.count})</option>
          ))}
        </select>
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
                {!!(p.tags || []).length && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 2, justifyContent: "center", marginTop: 2 }}>
                    {(p.tags as string[]).slice(0, 3).map(t => (
                      <span key={t} className="chip" title={t}>{t}</span>
                    ))}
                    {(p.tags as string[]).length > 3 && <span className="chip muted">+{(p.tags as string[]).length - 3}</span>}
                  </div>
                )}
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
                <button className="btn danger" onClick={remove}>Удалить карточку</button>
                <button className="btn danger" onClick={erase}
                        title="SPEC §24: удалить карточку вместе с кадрами, эмбеддингами и файлами снимков">
                  Удалить с биометрией
                </button>
              </div>
              {enhMsg && <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>{enhMsg}</div>}
              <div style={{ marginTop: 12 }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                  {/* Молчаливый отказ здесь опаснее прочих: галочка просто
                      отскакивала обратно, а оператор оставался в уверенности,
                      что персона в watchlist и Telegram-оповещение придёт. */}
                  <input type="checkbox" style={{ width: "auto" }} checked={!!sel.alert_on_detection} onChange={async e => {
                    try {
                      const u = await api.personUpdate(sel.id, { alert_on_detection: e.target.checked });
                      setSel({ ...sel, ...u });
                    } catch (err: any) { toast(err.message, "err"); }
                  }} />
                  В watchlist (Telegram-оповещение при детекции)
                </label>
                <label>Теги</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
                  {(sel.tags || []).map((t: string) => (
                    <span key={t} className="chip">
                      {t}
                      <button className="chip-x" title="Снять тег"
                        onClick={() => saveTags(removeTag(sel.tags || [], t))}>×</button>
                    </span>
                  ))}
                  {!(sel.tags || []).length && <span className="muted" style={{ fontSize: 12 }}>Тегов нет</span>}
                </div>
                <div className="row" style={{ marginBottom: 12 }}>
                  {/* list=... даёт подсказку из уже заведённых тегов: без
                      неё на объекте заводят «подрядчик» и «подрядчики». */}
                  <input list="person-tag-catalog" placeholder="Добавить тег" value={tagDraft}
                    onChange={e => setTagDraft(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); onAddTag(); } }}
                    style={{ width: 220 }} />
                  <datalist id="person-tag-catalog">
                    {tagCatalog.map(t => <option key={t.tag} value={t.tag} />)}
                  </datalist>
                  <button className="btn secondary" onClick={onAddTag} disabled={!tagDraft.trim()}>Добавить</button>
                </div>
                <label>Заметки</label>
                {/* Поле неуправляемое (defaultValue): при отказе текст
                    остаётся на экране и выглядит сохранённым — без catch
                    заметка терялась совершенно молча. */}
                <textarea rows={3} defaultValue={sel.notes || ""} onBlur={async e => {
                  if (e.target.value !== (sel.notes || "")) {
                    try {
                      const u = await api.personUpdate(sel.id, { notes: e.target.value });
                      setSel({ ...sel, ...u });
                    } catch (err: any) { toast(err.message, "err"); }
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
