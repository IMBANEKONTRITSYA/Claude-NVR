import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, getUser, getRole, isPasswordExpired, clearAuth } from "../api";

const PASSWORD_HINT = "Минимум 10 символов, минимум 3 из 4: строчные, ЗАГЛАВНЫЕ, цифры, спецсимволы";

export function Profile() {
  const [oldP, setOldP] = useState("");
  const [newP, setNewP] = useState("");
  const [confirmP, setConfirmP] = useState("");
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const expired = isPasswordExpired();
  const nav = useNavigate();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newP.length < 10) { setMsg({ type: "err", text: PASSWORD_HINT }); return; }
    if (newP !== confirmP) { setMsg({ type: "err", text: "Пароли не совпадают" }); return; }
    setBusy(true); setMsg(null);
    try {
      await api.changePassword(oldP, newP);
      // Смена пароля отзывает на бэкенде все refresh-токены этого пользователя
      // (сигнал возможной компрометации, см. /api/auth/change-password) — в
      // том числе текущий, поэтому храня его дальше в localStorage, приведёт
      // к тихому разлогиниванию при следующем истечении access-токена. Ведём
      // на повторный вход сразу же, с понятным сообщением.
      clearAuth();
      nav("/login?reason=password-changed");
    } catch (e: any) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <h2>Профиль</h2>
      {expired && (
        <div className="card" style={{ maxWidth: 460, borderColor: "var(--red)", marginBottom: 12 }}>
          Срок действия пароля истёк — смените его, чтобы продолжить работу с системой.
        </div>
      )}
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
            <input type="password" value={newP} onChange={e => setNewP(e.target.value)} required minLength={10} />
            <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{PASSWORD_HINT}</div>
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
