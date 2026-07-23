import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { clearToken, getToken } from "./api";
import IncomingCallModal from "./components/IncomingCallModal";
import { SoftphoneProvider } from "./lib/softphoneContext";
import Agents from "./pages/Agents";
import Callers from "./pages/Callers";
import Calls from "./pages/Calls";
import Dashboard from "./pages/Dashboard";
import Emails from "./pages/Emails";
import FlowEditor from "./pages/FlowEditor";
import Flows from "./pages/Flows";
import Inbox from "./pages/Inbox";
import Login from "./pages/Login";
import Messages from "./pages/Messages";
import NumberDetail from "./pages/NumberDetail";
import Numbers from "./pages/Numbers";
import Settings from "./pages/Settings";

function Layout({ children }: { children: any }) {
  const nav = useNavigate();
  const link = ({ isActive }: { isActive: boolean }) => "navlink" + (isActive ? " active" : "");
  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>📞 Call Monitor</h1>

        <div className="navsection">Attribution</div>
        <NavLink to="/" end className={link}>Dashboard</NavLink>
        <NavLink to="/calls" className={link}>Calls</NavLink>
        <NavLink to="/callers" className={link}>Callers</NavLink>
        <NavLink to="/emails" className={link}>Email Log</NavLink>

        <div className="navsection">Platform</div>
        <NavLink to="/inbox" className={link}>Inbox</NavLink>
        <NavLink to="/numbers" className={link}>Numbers</NavLink>
        <NavLink to="/flows" className={link}>Call Flows</NavLink>
        <NavLink to="/messages" className={link}>Messages</NavLink>
        <NavLink to="/agents" className={link}>AI Agents</NavLink>

        <div className="navsection">System</div>
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
  // Ticket 18: the softphone provider sits ABOVE <Routes> so the single registration (and any
  // live call) survives navigation — mounting it inside a route element would tear the
  // softphone down on every page change. The global incoming-call popup rides alongside it so
  // a call to an unassigned DID can be answered from ANY page (Quo-style).
  return (
    <SoftphoneProvider>
      <IncomingCallModal />
      <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Protected><Dashboard /></Protected>} />
      <Route path="/calls" element={<Protected><Calls /></Protected>} />
      <Route path="/numbers" element={<Protected><Numbers /></Protected>} />
      <Route path="/numbers/:id" element={<Protected><NumberDetail /></Protected>} />
      <Route path="/flows" element={<Protected><Flows /></Protected>} />
      <Route path="/flows/:id" element={<Protected><FlowEditor /></Protected>} />
      <Route path="/messages" element={<Protected><Messages /></Protected>} />
      <Route path="/inbox" element={<Protected><Inbox /></Protected>} />
      <Route path="/agents" element={<Protected><Agents /></Protected>} />
      <Route path="/callers" element={<Protected><Callers /></Protected>} />
      <Route path="/emails" element={<Protected><Emails /></Protected>} />
        <Route path="/settings" element={<Protected><Settings /></Protected>} />
      </Routes>
    </SoftphoneProvider>
  );
}
