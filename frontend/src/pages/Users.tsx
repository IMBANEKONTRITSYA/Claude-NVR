import { useEffect, useState } from "react";
import { api } from "../api";

export function Users() {
  const [users, setUsers] = useState<any[]>([]);
  const [form, setForm] = useState({ username: "", password: "", role: "viewer" });
  const load = () => api.users().then(setUsers);
  useEffect(() => { load(); }, []);

  const add = async () => {
    try {
      await api.userAdd(form);
      setForm({ username: "", password: "", role: "viewer" });
      load();
    } catch (e: any) { alert(e.message); }
  };

  const del = async (id: number) => {
    if (!confirm("Удалить пользователя?")) return;
    await api.userDel(id);
    load();
  };

  return (
    <div>
      <h2>Пользователи</h2>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Добавить</h3>
        <div className="grid" style={{ gridTemplateColumns: "1fr 1fr 1fr auto", gap: 8, alignItems: "end" }}>
          <div><label>Логин</label><input value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} /></div>
          <div><label>Пароль</label><input type="password" value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} /></div>
          <div>
            <label>Роль</label>
            <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}>
              <option value="admin">Администратор</option>
              <option value="operator">Оператор</option>
              <option value="viewer">Наблюдатель</option>
            </select>
          </div>
          <button className="btn" onClick={add}>Добавить</button>
        </div>
      </div>

      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>Логин</th><th>Роль</th><th></th></tr></thead>
          <tbody>
            {users.map(u => (
              <tr key={u.id}>
                <td>{u.id}</td><td>{u.username}</td><td>{u.role}</td>
                <td><button className="btn danger" onClick={() => del(u.id)} disabled={u.username === "admin"}>Удалить</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
