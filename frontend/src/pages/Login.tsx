import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { login } from "../api";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const nav = useNavigate();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    try {
      await login(email, password);
      nav("/");
    } catch {
      setErr("Invalid credentials");
    }
  };

  return (
    <div className="loginwrap">
      <form className="card logincard" onSubmit={submit}>
        <h1 style={{ marginTop: 0 }}>📞 Call Monitor</h1>
        <div style={{ display: "grid", gap: 10 }}>
          <input placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          <input placeholder="password" type="password" value={password}
                 onChange={(e) => setPassword(e.target.value)} />
          {err && <div style={{ color: "var(--danger)" }}>{err}</div>}
          <button className="primary" type="submit">Sign in</button>
        </div>
      </form>
    </div>
  );
}
