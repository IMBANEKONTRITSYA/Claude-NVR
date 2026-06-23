import { useState } from "react";
import { api, getUser, getRole } from "../api";

export function Profile() {
  const [oldP, setOldP] = useState("");
  const [newP, setNewP] = useState("");
  const [confirmP, setConfirmP] = useState("");
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newP.length < 6) { setMsg({ type: "err", text: "Минимум 6 символов" }); return; }
    if (newP !== confirmP) { setMsg({ type: "err", text: "Пароли не совпадают" }); return; }
    setBusy(true); setMsg(null);
    try {
      await api.changePassword(oldP, newP);
      setMsg({ type: "ok", text: "Пароль обновлён" });
      setOldP(""); setNewP(""); setConfirmP("");
    } catch (e: any) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <h2>Профиль</h2>
      <div className="card" style={{ maxWidth: 460 }}>
        <div style={{ marginBottom: 12 }}>
          <div className="muted" style={{ fontSize: 12 }}>Пользователь</div>
          <div>{getUser()} <span className="muted">·</span> {getRole()}</div>
        </div>
        <form onSubmit={submit}>
          <h3>Смена пароля</h3>
          <div style={{ marginBottom: 10 }}>
            <label>Старый пароль</label>
            <input type="password" value={oldP} onChange={e => setOldP(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 10 }}>
            <label>Новый пароль</label>
            <input type="password" value={newP} onChange={e => setNewP(e.target.value)} required minLength={6} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label>Повторите новый пароль</label>
            <input type="password" value={confirmP} onChange={e => setConfirmP(e.target.value)} required />
          </div>
          {msg && <div style={{ marginBottom: 10, color: msg.type === "ok" ? "var(--green)" : "var(--red)" }}>{msg.text}</div>}
          <button className="btn" type="submit" disabled={busy}>{busy ? "Сохранение..." : "Сменить пароль"}</button>
        </form>
      </div>
    </div>
  );
}
