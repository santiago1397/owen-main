import { useEffect, useState } from "react";
import {
  Inbox as InboxIcon,
  LayoutDashboard,
  Menu,
  MessageSquare,
  MoreHorizontal,
  Phone,
} from "lucide-react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { clearToken, getToken } from "./api";
import InCallModal from "./components/InCallModal";
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
  const loc = useLocation();
  // Below 900px the sidebar is an off-canvas drawer (see styles.css "RESPONSIVE"). On desktop
  // the .open class is inert — the transform that hides the sidebar only exists inside the
  // media query — so this state has no effect on the desktop layout.
  const [navOpen, setNavOpen] = useState(false);

  // Close on navigation, otherwise the drawer stays over the page you just moved to.
  useEffect(() => setNavOpen(false), [loc.pathname]);

  // Escape closes it, and the page behind must not scroll while it's over the top.
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setNavOpen(false);
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [navOpen]);

  const link = ({ isActive }: { isActive: boolean }) => "navlink" + (isActive ? " active" : "");
  const tab = ({ isActive }: { isActive: boolean }) => "tabitem" + (isActive ? " active" : "");
  return (
    <div className="layout">
      <header className="topbar">
        <button className="navtoggle" onClick={() => setNavOpen((v) => !v)}
                aria-label="Menu" aria-expanded={navOpen}>
          <Menu size={20} />
        </button>
        <h1>📞 Call Monitor</h1>
      </header>
      {navOpen && <div className="navscrim" onClick={() => setNavOpen(false)} />}
      <aside className={"sidebar" + (navOpen ? " open" : "")}>
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

      {/* Bottom tab bar: the four routes this app is actually opened for, within thumb reach.
          Hidden above 560px, where the sidebar (or the drawer) is already the nav. The other
          seven routes stay in the drawer, which "More" opens — see docs/RESPONSIVE_SPEC.md §5. */}
      <nav className="tabbar" aria-label="Primary">
        <NavLink to="/" end className={tab}><LayoutDashboard size={20} /><span>Dashboard</span></NavLink>
        <NavLink to="/calls" className={tab}><Phone size={20} /><span>Calls</span></NavLink>
        <NavLink to="/inbox" className={tab}><InboxIcon size={20} /><span>Inbox</span></NavLink>
        <NavLink to="/messages" className={tab}><MessageSquare size={20} /><span>Messages</span></NavLink>
        <button
          className={"tabitem" + (navOpen ? " active" : "")}
          onClick={() => setNavOpen((v) => !v)}
          aria-expanded={navOpen}
        >
          <MoreHorizontal size={20} /><span>More</span>
        </button>
      </nav>
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
      {/* In-call controls (hang up / mute / keypad) follow the call across every page — the
          only hang-up button used to live inside InCallBar, which one page renders. */}
      <InCallModal />
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
