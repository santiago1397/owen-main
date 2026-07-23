import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";

// Two-pane SMS inbox (Ticket 09): threads (grouped by number+caller) on the left, the
// selected conversation on the right. Polls every 5s (no websocket). Inbound-only for now —
// the composer is present but DISABLED; per-number outbound send arrives in Ticket 10.
const POLL_MS = 5000;

type Thread = {
  number_id: string | null;
  caller_id: string | null;
  caller_number: string | null;
  number_phone: string | null;
  number_label: string | null;
  campaign_name: string | null;
  provider: string | null;
  last_body: string | null;
  last_direction: string | null;
  last_at: string | null;
  message_count: number;
};

type Msg = {
  id: string;
  direction: string | null;
  body: string | null;
  status: string | null;
  num_media: number;
  media_urls: string[];
  received_at: string | null;
};

const threadKey = (t: { number_id: string | null; caller_id: string | null }) =>
  `${t.number_id ?? ""}:${t.caller_id ?? ""}`;

function Conversation({ thread }: { thread: Thread }) {
  const params = {
    number_id: thread.number_id ?? undefined,
    caller_id: thread.caller_id ?? undefined,
  };
  const { data: messages } = useQuery<Msg[]>({
    queryKey: ["messageThread", threadKey(thread)],
    queryFn: () => api.messageThread(params),
    refetchInterval: POLL_MS,
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ borderBottom: "1px solid var(--border, #2a2a2a)", paddingBottom: 8, marginBottom: 8 }}>
        <div style={{ fontWeight: 600 }}>{thread.caller_number || "Unknown caller"}</div>
        <div className="muted" style={{ fontSize: 12 }}>
          to {thread.number_phone || "—"}
          {thread.number_label ? ` (${thread.number_label})` : ""}
          {thread.provider ? ` · ${thread.provider}` : ""}
        </div>
      </div>

      <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
        {(messages || []).map((m) => {
          const inbound = m.direction !== "outbound";
          return (
            <div
              key={m.id}
              style={{
                alignSelf: inbound ? "flex-start" : "flex-end",
                maxWidth: "75%",
                background: inbound ? "var(--bubble-caller, #1e2a3a)" : "var(--bubble-operator, #23331f)",
                borderRadius: 10,
                padding: "6px 10px",
              }}
            >
              {m.body && <div style={{ whiteSpace: "pre-wrap" }}>{m.body}</div>}
              {m.num_media > 0 && (
                <div style={{ marginTop: 4 }}>
                  {m.media_urls.map((u, i) => (
                    <a key={i} href={u} target="_blank" rel="noreferrer" className="muted" style={{ fontSize: 11, display: "block" }}>
                      📎 media {i + 1}
                    </a>
                  ))}
                </div>
              )}
              <div className="muted" style={{ fontSize: 10, marginTop: 2 }}>
                {m.received_at ? new Date(m.received_at).toLocaleString() : ""}
              </div>
            </div>
          );
        })}
        {messages && messages.length === 0 && <div className="muted">No messages in this thread.</div>}
      </div>

      <div style={{ marginTop: 8, borderTop: "1px solid var(--border, #2a2a2a)", paddingTop: 8 }}>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            style={{ flex: 1 }}
            placeholder="Sending is enabled per-number in a later release…"
            disabled
          />
          <button disabled title="Outbound send arrives in a later ticket">Send</button>
        </div>
      </div>
    </div>
  );
}

export default function Messages() {
  const [selected, setSelected] = useState<string | null>(null);
  const { data: threads } = useQuery<Thread[]>({
    queryKey: ["messageThreads"],
    queryFn: () => api.messageThreads(),
    refetchInterval: POLL_MS,
  });

  const active = (threads || []).find((t) => threadKey(t) === selected) || null;

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Messages</h2>
      <div className="card" style={{ display: "flex", gap: 0, padding: 0, height: "70vh", overflow: "hidden" }}>
        <div style={{ width: 300, borderRight: "1px solid var(--border, #2a2a2a)", overflowY: "auto" }}>
          {(threads || []).map((t) => {
            const key = threadKey(t);
            return (
              <div
                key={key}
                className={"clickable" + (key === selected ? " active" : "")}
                onClick={() => setSelected(key)}
                style={{
                  padding: "10px 12px",
                  borderBottom: "1px solid var(--border, #2a2a2a)",
                  background: key === selected ? "var(--bubble-caller, #1e2a3a)" : undefined,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontWeight: 600 }}>{t.caller_number || "Unknown"}</span>
                  <span className="muted" style={{ fontSize: 11 }}>
                    {t.last_at ? new Date(t.last_at).toLocaleDateString() : ""}
                  </span>
                </div>
                <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {t.last_body || "(no text)"}
                </div>
                <div className="muted" style={{ fontSize: 11 }}>
                  {t.number_label || t.number_phone || "—"} · {t.message_count} msg
                </div>
              </div>
            );
          })}
          {threads && threads.length === 0 && (
            <div className="muted" style={{ padding: 12 }}>No conversations yet.</div>
          )}
        </div>
        <div style={{ flex: 1, padding: 12, minWidth: 0 }}>
          {active ? (
            <Conversation thread={active} />
          ) : (
            <div className="placeholder" style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <div className="muted">Select a conversation.</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
