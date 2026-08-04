import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, setAuth } from "../api";

export function Login() {
  const [u, setU] = useState("admin");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const [params] = useSearchParams();
  const reason = params.get("reason");
  const notice =
    reason === "inactive" ? "Сессия завершена из-за длительного бездействия. Войдите снова." :
    reason === "password-changed" ? "Пароль изменён. Войдите с новым паролем." :
    null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(""); setBusy(true);
    try {
      const r = await api.login(u, p);
      setAuth(r.access_token, r.refresh_token, r.role, r.username, r.password_expired);
      nav(r.password_expired ? "/profile" : "/dashboard");
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ display: "grid", placeItems: "center", height: "100vh" }}>
      <form className="card" style={{ width: 320 }} onSubmit={submit}>
        <h2 style={{ marginTop: 0 }}>FaceWatch — вход</h2>
        {notice && <div style={{ color: "var(--red)", marginBottom: 10 }}>{notice}</div>}
        <div style={{ marginBottom: 10 }}>
          <label>Логин</label>
          <input value={u} onChange={e => setU(e.target.value)} autoFocus />
        </div>
        <div style={{ marginBottom: 16 }}>
          <label>Пароль</label>
          <input type="password" value={p} onChange={e => setP(e.target.value)} />
        </div>
        {err && <div style={{ color: "var(--red)", marginBottom: 10 }}>{err}</div>}
        <button className="btn" type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Вход..." : "Войти"}
        </button>
      </form>
    </div>
  );
}
