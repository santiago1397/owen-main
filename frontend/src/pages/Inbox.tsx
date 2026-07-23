import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  Check,
  Headphones,
  Info,
  MessageSquarePlus,
  Paperclip,
  Phone,
  PhoneIncoming,
  PhoneOutgoing,
  Plus,
  RotateCcw,
  SendHorizontal,
  Settings,
  X,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { API_BASE, api, ApiError } from "../api";
import InCallBar from "../components/InCallBar";

// Quo-style per-contact Inbox: one thread per CONTACT across all BulkVS DIDs, messages AND
// calls interleaved in one timeline. Scope = platform (bulkvs/asterisk) only; the legacy
// /messages page (per-number threads, all providers) is untouched. Polls every 5s.
const POLL_MS = 5000;

type DidOut = { number_id: string; phone_number: string | null } | null;

type Thread = {
  caller_id: string;
  contact_number: string | null;
  contact_name: string | null;
  company: string | null;
  role: string | null;
  last_at: string | null;
  last_kind: string | null;
  last_direction: string | null;
  last_preview: string | null;
  message_count: number;
  call_count: number;
  unread_count: number;
  open: boolean;
  responded: boolean;
  sticky_number: DidOut;
  call_from: DidOut;
  sms_from: DidOut;
  sms_via_fallback: boolean;
  sms_disabled_reason: string | null;
};

type TimelineItem = {
  type: "message" | "call";
  id: string;
  direction: string | null;
  body?: string | null;
  status: string | null;
  num_media?: number;
  media_urls?: string[];
  duration_seconds?: number | null;
  at: string | null;
  our_number: string | null;
  recording_id?: string | null;
};

type ThreadDetail = {
  contact: {
    caller_id: string;
    phone_number: string | null;
    name: string | null;
    company: string | null;
    role: string | null;
    first_seen_at: string | null;
    total_calls: number;
  };
  items: TimelineItem[];
  notes: { id: string; body: string; author: string | null; created_at: string }[];
};

// --- tiny presentation helpers -----------------------------------------------------------

const AVATAR_COLORS = ["#e0559b", "#5b8def", "#2fbf71", "#f0a03c", "#9b6cf0", "#ef6461", "#3cc8c8"];
function avatarColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}
function initials(name: string | null, number: string | null): string {
  if (name && name.trim()) {
    const parts = name.trim().split(/\s+/);
    return (parts[0][0] + (parts[1]?.[0] || "")).toUpperCase();
  }
  const d = (number || "").replace(/\D/g, "");
  return d ? d.slice(-2) : "#";
}
function displayName(t: { contact_name?: string | null; contact_number?: string | null }): string {
  return t.contact_name || fmtPhone(t.contact_number) || "Unknown";
}
function fmtPhone(p: string | null | undefined): string {
  if (!p) return "";
  const d = p.replace(/\D/g, "");
  if (d.length === 11 && d.startsWith("1"))
    return `(${d.slice(1, 4)}) ${d.slice(4, 7)}-${d.slice(7)}`;
  if (d.length === 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  return p;
}
function fmtListTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function dayLabel(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "Today";
  if (d.toDateString() === yest.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
function fmtDur(s: number | null | undefined): string {
  if (s == null) return "";
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

function Avatar({ name, number, size = 36 }: { name: string | null; number: string | null; size?: number }) {
  const key = number || name || "?";
  return (
    <div
      className="quo-avatar"
      style={{ width: size, height: size, minWidth: size, background: avatarColor(key), fontSize: size * 0.38 }}
    >
      {initials(name, number)}
    </div>
  );
}

function RecordingPlayer({ recordingId }: { recordingId: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    api.playUrl(recordingId).then((r: any) => setUrl(API_BASE + r.url)).catch(() => setUrl(null));
  }, [recordingId]);
  if (!url) return <span className="muted" style={{ fontSize: 11 }}>loading…</span>;
  return <audio controls src={url} style={{ height: 28, maxWidth: 220 }} />;
}

// --- conversation timeline ---------------------------------------------------------------

function Timeline({ items }: { items: TimelineItem[] }) {
  const endRef = useRef<HTMLDivElement>(null);
  const lastId = items.length ? items[items.length - 1].id : null;
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [lastId]);

  let lastDay = "";
  return (
    <div className="quo-timeline">
      {items.map((it) => {
        const day = it.at ? dayLabel(it.at) : "";
        const divider = day && day !== lastDay;
        lastDay = day || lastDay;
        const time = it.at
          ? new Date(it.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
          : "";
        return (
          <div key={`${it.type}-${it.id}`} style={{ display: "contents" }}>
            {divider && <div className="quo-day">{day}</div>}
            {it.type === "call" ? (
              <div className="quo-callchip">
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  {it.direction === "outbound" ? <PhoneOutgoing size={13} /> : <PhoneIncoming size={13} />}
                </span>
                <span>
                  {it.direction === "outbound" ? "Outgoing call" : "Incoming call"}
                  {it.status && it.status !== "completed" ? ` · ${it.status}` : ""}
                  {it.duration_seconds != null ? ` · ${fmtDur(it.duration_seconds)}` : ""}
                </span>
                {it.recording_id && <RecordingPlayer recordingId={it.recording_id} />}
                <span style={{ fontSize: 10.5 }}>{time}</span>
              </div>
            ) : (
              <div className={"quo-msgrow " + (it.direction === "outbound" ? "out" : "in")}>
                <div className="quo-bubble">
                  {it.body}
                  {(it.num_media || 0) > 0 &&
                    (it.media_urls || []).map((u, i) => (
                      <a key={i} href={u} target="_blank" rel="noreferrer"
                         style={{ display: "block", fontSize: 11, color: "inherit", textDecoration: "underline" }}>
                        <Paperclip size={11} style={{ verticalAlign: "-1px", marginRight: 3 }} />
                        media {i + 1}
                      </a>
                    ))}
                </div>
                <div className="quo-msgmeta">
                  {time}
                  {it.direction === "outbound" && it.status ? ` · ${it.status}` : ""}
                </div>
              </div>
            )}
          </div>
        );
      })}
      {items.length === 0 && <div className="quo-day">No activity yet — say hi</div>}
      <div ref={endRef} />
    </div>
  );
}

// --- contact side panel ------------------------------------------------------------------

function ContactPanel({
  detail,
  onCall,
  open,
  onClose,
}: {
  detail: ThreadDetail;
  onCall: () => void;
  open?: boolean;
  onClose?: () => void;
}) {
  const qc = useQueryClient();
  const c = detail.contact;
  const [name, setName] = useState(c.name || "");
  const [company, setCompany] = useState(c.company || "");
  const [role, setRole] = useState(c.role || "");
  const [note, setNote] = useState("");
  useEffect(() => {
    setName(c.name || "");
    setCompany(c.company || "");
    setRole(c.role || "");
  }, [c.caller_id]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["inboxThread", c.caller_id] });
    qc.invalidateQueries({ queryKey: ["inboxThreads"] });
  };
  const save = (body: { name?: string; company?: string; role?: string }) =>
    api.inboxUpdateContact(c.caller_id, body).then(invalidate).catch(() => {});
  const addNote = () => {
    const body = note.trim();
    if (!body) return;
    api.inboxAddNote(c.caller_id, body).then(() => {
      setNote("");
      invalidate();
    });
  };

  return (
    <div className={"quo-side" + (open ? " open" : "")}>
      {/* Close control for the mobile slide-over; hidden on desktop. */}
      <button className="quo-iconbtn quo-sideclose" title="Close" aria-label="Close contact details"
              onClick={onClose}>
        <X size={18} />
      </button>
      <div style={{ display: "flex", justifyContent: "center" }}>
        <Avatar name={c.name} number={c.phone_number} size={64} />
      </div>
      <h3>{c.name || fmtPhone(c.phone_number) || "Unknown"}</h3>
      <div style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 10 }}>
        <button className="quo-iconbtn" style={{ border: "1px solid var(--q-border)" }} title="Call" onClick={onCall}><Phone size={16} /></button>
      </div>

      <div className="quo-sec">Contact</div>
      <div className="quo-kv">
        <span className="k">Name</span>
        <input value={name} placeholder="Set a name" onChange={(e) => setName(e.target.value)}
               onBlur={() => name !== (c.name || "") && save({ name })} />
      </div>
      <div className="quo-kv">
        <span className="k">Company</span>
        <input value={company} placeholder="Set a company" onChange={(e) => setCompany(e.target.value)}
               onBlur={() => company !== (c.company || "") && save({ company })} />
      </div>
      <div className="quo-kv">
        <span className="k">Role</span>
        <input value={role} placeholder="Set a role" onChange={(e) => setRole(e.target.value)}
               onBlur={() => role !== (c.role || "") && save({ role })} />
      </div>
      <div className="quo-kv">
        <span className="k">Mobile</span>
        <span>{fmtPhone(c.phone_number) || "—"}</span>
      </div>
      <div className="quo-kv">
        <span className="k">Calls</span>
        <span>{c.total_calls}</span>
      </div>
      <div className="quo-kv">
        <span className="k">First seen</span>
        <span>{c.first_seen_at ? new Date(c.first_seen_at).toLocaleDateString() : "—"}</span>
      </div>

      <div className="quo-sec">Notes ({detail.notes.length})</div>
      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        <input style={{ flex: 1, background: "var(--q-panel2)", border: "1px solid var(--q-border)",
                        borderRadius: 10, padding: "7px 10px" }}
               placeholder="Write a note…" value={note}
               onChange={(e) => setNote(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && addNote()} />
        <button className="quo-send" disabled={!note.trim()} onClick={addNote}><Plus size={16} /></button>
      </div>
      {detail.notes.map((n) => (
        <div key={n.id} className="quo-note">
          <div>{n.body}</div>
          <div className="muted" style={{ fontSize: 10.5, marginTop: 4, display: "flex", justifyContent: "space-between" }}>
            <span>{n.author || ""} · {new Date(n.created_at).toLocaleString()}</span>
            <a style={{ cursor: "pointer" }}
               onClick={() => api.inboxDeleteNote(n.id).then(invalidate)}><X size={12} /></a>
          </div>
        </div>
      ))}
    </div>
  );
}

// --- main page ---------------------------------------------------------------------------

type ListTab = "chats" | "calls";
type OpenFilter = "open" | "all" | "closed";

export default function Inbox() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<ListTab>("chats");
  const [openFilter, setOpenFilter] = useState<OpenFilter>("open");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [unrespondedOnly, setUnrespondedOnly] = useState(false);
  // Selection lives in the URL (?c=<caller_id>), not component state. Below 900px the list and
  // the conversation are separate screens, so iOS edge-swipe-back and the browser back button
  // have to move between them — they can only do that if the pane is a history entry. It also
  // makes a conversation deep-linkable. Desktop is unaffected: both panes render regardless.
  const [params, setParams] = useSearchParams();
  const selected = params.get("c");
  const setSelected = (id: string | null) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id) next.set("c", id);
        else next.delete("c");
        return next;
      },
      // Opening a thread pushes, so Back returns to the list; closing replaces so you don't
      // have to press Back twice to leave the Inbox.
      { replace: !id }
    );
  };
  const [showPhone, setShowPhone] = useState(false);
  const [showNewChat, setShowNewChat] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [callNote, setCallNote] = useState<string | null>(null);

  const { data: threads } = useQuery<Thread[]>({
    queryKey: ["inboxThreads"],
    queryFn: () => api.inboxThreads(),
    refetchInterval: POLL_MS,
  });
  const { data: settings } = useQuery<{ default_number_id: string | null; numbers: any[] }>({
    queryKey: ["inboxSettings"],
    queryFn: () => api.inboxSettings(),
  });

  const active = (threads || []).find((t) => t.caller_id === selected) || null;

  const filtered = useMemo(() => {
    let list = threads || [];
    if (openFilter === "open") list = list.filter((t) => t.open);
    if (openFilter === "closed") list = list.filter((t) => !t.open);
    if (unreadOnly) list = list.filter((t) => t.unread_count > 0);
    if (unrespondedOnly) list = list.filter((t) => !t.responded);
    return list;
  }, [threads, openFilter, unreadOnly, unrespondedOnly]);

  const placeCall = async (t: Thread) => {
    const from = t.call_from?.phone_number;
    const to = t.contact_number;
    if (!from || !to) {
      setCallNote("No BulkVS caller-ID available for this contact.");
      return;
    }
    setShowPhone(true);
    setCallNote(null);
    try {
      const res = await api.outboundCall(to, from);
      setCallNote(
        `Calling ${fmtPhone(to)} from ${fmtPhone(from)} — your softphone will ring.` +
          (res?.warnings?.length ? ` ⚠ ${res.warnings.join(" · ")}` : "")
      );
    } catch (e: any) {
      setCallNote(`Call error: ${String(e?.message || e)}`);
    }
  };

  return (
    // has-active drives the below-900px single-pane switch: with a thread open the list is
    // hidden and the conversation fills the screen; with none open, the reverse. Desktop shows
    // both either way — the rule only exists inside the media query.
    <div className={"quo" + (active ? " has-active" : "")}>
      {/* left rail: tabs + filters + thread list / call log */}
      <div className="quo-list">
        <div className="quo-listhead">
          <button className={"qtab" + (tab === "chats" ? " active" : "")} onClick={() => setTab("chats")}>Chats</button>
          <button className={"qtab" + (tab === "calls" ? " active" : "")} onClick={() => setTab("calls")}>Calls</button>
          <div style={{ flex: 1 }} />
          <button className="quo-iconbtn" title="Softphone" onClick={() => setShowPhone((v) => !v)}><Phone size={16} /></button>
          <button className="quo-iconbtn" title="New chat" onClick={() => setShowNewChat(true)}><MessageSquarePlus size={16} /></button>
          <button className="quo-iconbtn" title="Inbox settings" onClick={() => setShowSettings(true)}><Settings size={16} /></button>
        </div>

        {tab === "chats" && (
          <>
            <div className="quo-pills">
              <select className="quo-pill" value={openFilter}
                      onChange={(e) => setOpenFilter(e.target.value as OpenFilter)}
                      style={{ appearance: "none" }}>
                <option value="open">Open</option>
                <option value="all">All</option>
                <option value="closed">Closed</option>
              </select>
              <button className={"quo-pill" + (unreadOnly ? " on" : "")}
                      onClick={() => setUnreadOnly((v) => !v)}>Unread</button>
              <button className={"quo-pill" + (unrespondedOnly ? " on" : "")}
                      onClick={() => setUnrespondedOnly((v) => !v)}>Unresponded</button>
            </div>
            <div className="quo-threads">
              {filtered.map((t) => (
                <div key={t.caller_id}
                     className={"quo-thread" + (t.caller_id === selected ? " sel" : "")}
                     onClick={() => setSelected(t.caller_id)}>
                  <Avatar name={t.contact_name} number={t.contact_number} />
                  <div className="quo-tmain">
                    <div className="quo-tname">{displayName(t)}</div>
                    <div className="quo-tprev">
                      {t.last_kind === "call" || t.last_direction !== "outbound" ? "" : "You: "}
                      {t.last_preview || "(no text)"}
                    </div>
                  </div>
                  <div className="quo-tside">
                    <span className="quo-ttime">{fmtListTime(t.last_at)}</span>
                    {t.unread_count > 0 && <span className="quo-unread">{t.unread_count}</span>}
                  </div>
                </div>
              ))}
              {filtered.length === 0 && (
                <div className="placeholder" style={{ color: "var(--q-muted)" }}>No conversations.</div>
              )}
            </div>
          </>
        )}

        {tab === "calls" && (
          <CallLog
            onOpenThread={(phone) => {
              const t = (threads || []).find((x) => x.contact_number === phone);
              if (t) {
                setTab("chats");
                setSelected(t.caller_id);
              }
            }}
          />
        )}
      </div>

      {/* center: conversation */}
      <div className="quo-convo">
        {showPhone && (
          <div style={{ borderBottom: "1px solid var(--q-border)", padding: 8 }}>
            <InCallBar />
            {callNote && <div className="muted" style={{ fontSize: 12, padding: "4px 8px" }}>{callNote}</div>}
          </div>
        )}
        {active ? (
          <Conversation
            key={active.caller_id}
            thread={active}
            onCall={() => void placeCall(active)}
            onBack={() => setSelected(null)}
          />
        ) : (
          <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--q-muted)" }}>
            Select a conversation
          </div>
        )}
      </div>

      {showNewChat && (
        <NewChatModal
          onClose={() => setShowNewChat(false)}
          onSent={(callerId) => {
            setShowNewChat(false);
            setTab("chats");
            setSelected(callerId);
            qc.invalidateQueries({ queryKey: ["inboxThreads"] });
          }}
        />
      )}
      {showSettings && settings && (
        <SettingsModal settings={settings} onClose={() => setShowSettings(false)} />
      )}
    </div>
  );
}

// --- conversation (center pane + side panel) ---------------------------------------------

function Conversation({
  thread,
  onCall,
  onBack,
}: {
  thread: Thread;
  onCall: () => void;
  onBack?: () => void;
}) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  // The 300px contact rail can't sit beside the conversation on a phone, so below 900px it
  // becomes a slide-over opened from the header. Closed by default; inert on desktop, where
  // the panel is always in the flow.
  const [showContact, setShowContact] = useState(false);
  useEffect(() => setShowContact(false), [thread.caller_id]);
  const { data: detail } = useQuery<ThreadDetail>({
    queryKey: ["inboxThread", thread.caller_id],
    queryFn: () => api.inboxThread(thread.caller_id),
    refetchInterval: POLL_MS,
  });

  // Opening the thread — and any new activity while it's open — marks it read (global state).
  const newestAt = detail?.items.length ? detail.items[detail.items.length - 1].at : null;
  useEffect(() => {
    if (!detail) return;
    api.inboxMarkRead(thread.caller_id).then(() =>
      qc.invalidateQueries({ queryKey: ["inboxThreads"] })
    );
  }, [thread.caller_id, newestAt]);

  const send = useMutation({
    mutationFn: () =>
      api.inboxSend({ contact: thread.contact_number as string, body: draft.trim() }),
    onSuccess: () => {
      setDraft("");
      qc.invalidateQueries({ queryKey: ["inboxThread", thread.caller_id] });
      qc.invalidateQueries({ queryKey: ["inboxThreads"] });
    },
  });
  const sendError =
    send.error instanceof ApiError ? send.error.message : send.error ? String(send.error) : null;
  const canSend = !!thread.sms_from && !!thread.contact_number;
  const submit = () => {
    if (canSend && draft.trim() && !send.isPending) send.mutate();
  };

  const toggleClosed = () =>
    api.inboxSetClosed(thread.caller_id, thread.open).then(() =>
      qc.invalidateQueries({ queryKey: ["inboxThreads"] })
    );

  return (
    <>
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
          <div className="quo-chead">
            {/* Back to the thread list — only rendered as a control below 900px. */}
            {onBack && (
              <button className="quo-iconbtn quo-back" title="Back" aria-label="Back to conversations"
                      onClick={onBack}>
                <ArrowLeft size={18} />
              </button>
            )}
            <Avatar name={thread.contact_name} number={thread.contact_number} size={32} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 700, fontSize: 14 }}>{displayName(thread)}</div>
              <div className="muted" style={{ fontSize: 11.5, color: "var(--q-muted)" }}>
                {fmtPhone(thread.contact_number)}
                {thread.sticky_number?.phone_number
                  ? ` · via ${fmtPhone(thread.sticky_number.phone_number)}`
                  : ""}
              </div>
            </div>
            <div style={{ flex: 1 }} />
            {/* Opens the contact slide-over; hidden on desktop where the panel is always shown. */}
            <button className="quo-iconbtn quo-contacttoggle" title="Contact details"
                    aria-label="Contact details" onClick={() => setShowContact(true)}>
              <Info size={16} />
            </button>
            <button className="quo-iconbtn" title="Call" onClick={onCall}><Phone size={16} /></button>
            <button className="quo-iconbtn" title={thread.open ? "Mark done (close)" : "Reopen"}
                    onClick={toggleClosed}>
              {thread.open ? <Check size={16} /> : <RotateCcw size={16} />}
            </button>
          </div>

          <Timeline items={detail?.items || []} />

          <div className="quo-composer">
            <div className="box">
              <textarea
                rows={1}
                placeholder={
                  canSend
                    ? "Write a message…"
                    : thread.sms_disabled_reason || "SMS unavailable for this contact"
                }
                value={draft}
                disabled={!canSend || send.isPending}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submit();
                  }
                }}
              />
              <button className="quo-send" onClick={submit}
                      disabled={!canSend || !draft.trim() || send.isPending}>
                {send.isPending ? "…" : <SendHorizontal size={16} />}
              </button>
            </div>
            <div className="muted" style={{ fontSize: 11, marginTop: 4, color: "var(--q-muted)" }}>
              {canSend
                ? `Sending from ${fmtPhone(thread.sms_from?.phone_number)}` +
                  (thread.sms_via_fallback ? " (default number — this contact's number can't text)" : "")
                : thread.sms_disabled_reason || ""}
            </div>
            {sendError && (
              <div style={{ fontSize: 11.5, marginTop: 2, color: "var(--danger, #ff5c6c)" }}>{sendError}</div>
            )}
          </div>
        </div>

        {/* Scrim only exists below 900px (styles.css), where the panel slides over. */}
        {showContact && <div className="quo-sidescrim" onClick={() => setShowContact(false)} />}
        {detail && (
          <ContactPanel
            detail={detail}
            onCall={onCall}
            open={showContact}
            onClose={() => setShowContact(false)}
          />
        )}
      </div>
    </>
  );
}

// --- calls tab (flat platform call log) --------------------------------------------------

function CallLog({ onOpenThread }: { onOpenThread: (phone: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data } = useQuery<any>({
    queryKey: ["inboxCalls"],
    queryFn: () => api.calls({ provider_group: "platform", page: 1, page_size: 50 }),
    refetchInterval: POLL_MS,
  });
  const { data: callDetail } = useQuery<any>({
    queryKey: ["inboxCallDetail", expanded],
    queryFn: () => api.call(expanded as string),
    enabled: !!expanded,
  });
  const rows = data?.items || [];
  return (
    <div className="quo-threads">
      {rows.map((c: any) => (
        <div key={c.id}>
          <div className="quo-callrow" style={{ cursor: "pointer" }}
               onClick={() => setExpanded(expanded === c.id ? null : c.id)}>
            <span style={{ display: "inline-flex", alignItems: "center" }}>
              {c.direction === "outbound"
                ? <PhoneOutgoing size={14} color="var(--q-muted)" />
                : <PhoneIncoming size={14} color="var(--q-muted)" />}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>{fmtPhone(c.caller_number) || "Unknown"}</div>
              <div style={{ color: "var(--q-muted)", fontSize: 11.5 }}>
                {c.direction === "outbound" ? "You called" : c.status || "Call"} · {fmtDur(c.duration_seconds)}
              </div>
            </div>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2 }}>
              <span className="quo-ttime">{fmtListTime(c.started_at)}</span>
              {c.has_recording && <Headphones size={13} color="var(--q-muted)" />}
            </div>
          </div>
          {expanded === c.id && (
            <div style={{ padding: "6px 14px 12px", borderBottom: "1px solid var(--q-border)" }}>
              {callDetail?.recordings?.length ? (
                <RecordingPlayer recordingId={callDetail.recordings[0].id} />
              ) : (
                <span className="muted" style={{ fontSize: 11.5 }}>No recording.</span>
              )}
              {c.caller_number && (
                <div style={{ marginTop: 6 }}>
                  <button className="quo-pill" onClick={() => onOpenThread(c.caller_number)}>
                    Open conversation →
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      ))}
      {rows.length === 0 && (
        <div className="placeholder" style={{ color: "var(--q-muted)" }}>No platform calls yet.</div>
      )}
    </div>
  );
}

// --- new chat + settings modals ----------------------------------------------------------

function NewChatModal({ onClose, onSent }: { onClose: () => void; onSent: (callerId: string) => void }) {
  const [to, setTo] = useState("");
  const [body, setBody] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const start = async () => {
    if (!to.trim() || !body.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await api.inboxSend({ contact: to.trim(), body: body.trim() });
      onSent(res.caller_id);
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="quo-modal" onClick={onClose}>
      <div className="inner" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 700, fontSize: 15 }}>New message</div>
        <input placeholder="Phone number, e.g. (305) 555-0123" value={to}
               style={{ borderRadius: 10, padding: "8px 10px" }}
               onChange={(e) => setTo(e.target.value)} autoFocus />
        <textarea placeholder="Message… (sent from the default number)" rows={3} value={body}
                  style={{ borderRadius: 10, padding: "8px 10px", resize: "vertical" }}
                  onChange={(e) => setBody(e.target.value)} />
        {err && <div style={{ color: "var(--danger, #ff5c6c)", fontSize: 12 }}>{err}</div>}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="quo-pill" onClick={onClose}>Cancel</button>
          <button className="quo-send" disabled={busy || !to.trim() || !body.trim()} onClick={() => void start()}>
            {busy ? "Sending…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function SettingsModal({
  settings,
  onClose,
}: {
  settings: { default_number_id: string | null; numbers: any[] };
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [val, setVal] = useState(settings.default_number_id || "");
  const save = async () => {
    await api.inboxSetDefaultNumber(val || null);
    qc.invalidateQueries({ queryKey: ["inboxSettings"] });
    qc.invalidateQueries({ queryKey: ["inboxThreads"] });
    onClose();
  };
  return (
    <div className="quo-modal" onClick={onClose}>
      <div className="inner" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 700, fontSize: 15 }}>Inbox settings</div>
        <div className="muted" style={{ fontSize: 12.5, color: "var(--q-muted)" }}>
          Default number — used for new conversations and as the SMS fallback when a
          contact's usual number can't text (no A2P 10DLC).
        </div>
        <select value={val} onChange={(e) => setVal(e.target.value)}
                style={{ borderRadius: 10, padding: "8px 10px" }}>
          <option value="">— none —</option>
          {settings.numbers.map((n) => (
            <option key={n.id} value={n.id}>
              {fmtPhone(n.phone_number)}
              {n.friendly_name ? ` (${n.friendly_name})` : ""}
              {n.sms_ok ? " · SMS ✓" : " · calls only"}
            </option>
          ))}
        </select>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="quo-pill" onClick={onClose}>Cancel</button>
          <button className="quo-send" onClick={() => void save()}>Save</button>
        </div>
      </div>
    </div>
  );
}
