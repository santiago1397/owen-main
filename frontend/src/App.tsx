import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { clearToken, getToken } from "./api";
import Callers from "./pages/Callers";
import Calls from "./pages/Calls";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import Numbers from "./pages/Numbers";
import Settings from "./pages/Settings";

function Layout({ children }: { children: any }) {
  const nav = useNavigate();
  const link = ({ isActive }: { isActive: boolean }) => "navlink" + (isActive ? " active" : "");
  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>📞 Call Monitor</h1>
        <NavLink to="/" end className={link}>Dashboard</NavLink>
        <NavLink to="/calls" className={link}>Calls</NavLink>
        <NavLink to="/numbers" className={link}>Numbers</NavLink>
        <NavLink to="/callers" className={link}>Callers</NavLink>
        <NavLink to="/settings" className={link}>Settings</NavLink>
        <div style={{ flex: 1 }} />
        <button onClick={() => { clearToken(); nav("/login"); }}>Log out</button>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

function Protected({ children }: { children: any }) {
  return getToken() ? <Layout>{children}</Layout> : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Protected><Dashboard /></Protected>} />
      <Route path="/calls" element={<Protected><Calls /></Protected>} />
      <Route path="/numbers" element={<Protected><Numbers /></Protected>} />
      <Route path="/callers" element={<Protected><Callers /></Protected>} />
      <Route path="/settings" element={<Protected><Settings /></Protected>} />
    </Routes>
  );
}
