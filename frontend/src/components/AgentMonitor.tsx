import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";

// Supervisor monitoring for live AI-agent calls (AI_AGENT_SPEC D4/D5).
//
// Two actions, deliberately only two:
//   LISTEN     rings your softphone and bridges you to a one-way snoop. The caller and the
//              agent hear nothing of you.
//   TAKE OVER  the agent stops, you are bridged to the caller, and the call is marked
//              HUMAN-OWNED — after which no automated path may touch it. Without that flag
//              the flow would route the ended agent node into voicemail, over the top of a
//              conversation you are now having.
//
// There is no "whisper/coach" mode: it exists to coach a human, and you cannot coach an LLM
// mid-turn by talking to it.

type Session = {
  linkedid: string;
  session_uuid: string;
  call_channel_id?: string | null;
  turns?: number;
  duration_s?: number;
};

export default function AgentMonitor() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");

  // Short poll: these are live calls, and a stale list is worse than none — you would be
  // offering to seize a call that ended a minute ago.
  const { data, isError } = useQuery<{ sessions: Session[] }>({
    queryKey: ["monitor-active"],
    queryFn: api.monitorActive,
    refetchInterval: 5000,
  });

  const listen = useMutation({
    mutationFn: (lid: string) => api.monitorListen(lid),
    onSuccess: () => setStatus("Your phone is ringing — answer it to listen in."),
    onError: (e: any) => setStatus(`Listen failed: ${e.message}`),
  });
  const takeover = useMutation({
    mutationFn: (lid: string) => api.monitorTakeover(lid),
    onSuccess: (r: any) => {
      setStatus(`Taking over — answer your phone. This call is now yours (${r.owner}).`);
      qc.invalidateQueries({ queryKey: ["monitor-active"] });
    },
    onError: (e: any) => setStatus(`Take-over failed: ${e.message}`),
  });

  // Telephony off, or the voice service unreachable. Not worth a scary empty panel.
  if (isError) return null;
  const sessions = data?.sessions || [];

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="toolbar" style={{ gap: 8 }}>
        <h3 style={{ margin: 0, flex: 1 }}>Live AI calls</h3>
        <span className="muted">{sessions.length ? `${sessions.length} in progress` : "none right now"}</span>
      </div>

      {sessions.length === 0 ? (
        <p className="muted" style={{ marginTop: 8 }}>
          When an agent is on a call it appears here, and you can listen in or take it over.
        </p>
      ) : (
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr><th>Call</th><th>Turns</th><th>Duration</th><th /></tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.session_uuid}>
                <td style={{ fontFamily: "monospace" }}>{s.linkedid}</td>
                <td>{s.turns ?? 0}</td>
                <td>{s.duration_s != null ? `${Math.round(s.duration_s)}s` : "—"}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button disabled={listen.isPending} onClick={() => listen.mutate(s.linkedid)}>
                    Listen
                  </button>{" "}
                  <button
                    className="primary"
                    disabled={takeover.isPending}
                    onClick={() => {
                      // Irreversible: the agent is dismissed and the call becomes yours.
                      if (confirm("Take this call over? The agent stops immediately and the call is handed to you.")) {
                        takeover.mutate(s.linkedid);
                      }
                    }}
                  >
                    Take over
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {status && <p className="muted" style={{ marginTop: 8 }}>{status}</p>}
    </div>
  );
}
