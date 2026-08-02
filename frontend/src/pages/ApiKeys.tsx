import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api } from "../api";

/** API keys for the AI API (/api/ai/*) — read-only machine credentials handed to AI agents
 *  and outside integrations.
 *
 *  The one thing this screen must get right: the plaintext secret exists for exactly one
 *  render. The server stores only a SHA-256 hash and has no endpoint that can return it, so
 *  the create flow blocks on an explicit acknowledgement rather than a toast that can be
 *  missed. Everything else here is a table.
 */

const SCOPE_HELP: Record<string, string> = {
  read: "Counts, durations, series and pipeline health. The safe default.",
  content: "Call transcripts, AI summaries, SMS bodies and customer names/addresses.",
  sql: "Run read-only SQL. Requires 'content' too — a role that can SELECT can read transcripts.",
  logs: "Errors, dead jobs and failed relays. Error text often contains phone numbers.",
};

function ScopeBadges({ scopes }: { scopes: string[] }) {
  if (!scopes?.length) return <span className="muted">none</span>;
  return (
    <span style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
      {scopes.map((s) => (
        <span key={s} className={"badge" + (s === "read" ? "" : " prov")}>{s}</span>
      ))}
    </span>
  );
}

function CreateModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [expires, setExpires] = useState("");
  const [issued, setIssued] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api.createApiKey({
        name: name.trim(),
        scopes,
        expires_in_days: expires ? Number(expires) : null,
      }),
    onSuccess: (res: any) => {
      setIssued(res.key);
      qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
  });

  const toggle = (s: string) =>
    setScopes((prev) => {
      const next = prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s];
      // Ad-hoc SQL can read anything the query role can, transcripts included. Ticking `sql`
      // without `content` would produce a key that 403s on the only endpoint it was made for.
      if (s === "sql" && !prev.includes("sql") && !next.includes("content")) next.push("content");
      return next;
    });

  // The secret is on screen and nowhere else. Closing by clicking the backdrop would throw it
  // away silently, so once issued the only way out is the acknowledge button.
  const dismissable = issued === null;

  return (
    <>
      <div className="overlay" onClick={dismissable ? onClose : undefined} />
      <div className="drawer">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>{issued ? "Key created" : "New API key"}</h3>
          {dismissable && <button onClick={onClose}>✕</button>}
        </div>

        {issued ? (
          <>
            <div className="card" style={{ marginTop: 12, borderColor: "var(--warn)" }}>
              <div className="l" style={{ marginBottom: 8 }}>Copy this now</div>
              <p className="muted" style={{ marginTop: 0 }}>
                This is the only time the key is shown. It is stored hashed and cannot be
                recovered — if you lose it, revoke this key and issue another.
              </p>
              <code
                className="mono"
                style={{ display: "block", wordBreak: "break-all", padding: 10,
                         background: "var(--panel2)", borderRadius: 8, marginBottom: 10 }}
              >
                {issued}
              </code>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(issued);
                  setCopied(true);
                }}
              >
                {copied ? "Copied ✓" : "Copy key"}
              </button>
            </div>

            <div className="card" style={{ marginTop: 12 }}>
              <div className="l" style={{ marginBottom: 8 }}>How to use it</div>
              <p className="muted" style={{ marginTop: 0, marginBottom: 8 }}>
                Give an AI the base URL and this key — it can learn the rest from{" "}
                <code className="mono">GET /api/ai/docs</code>.
              </p>
              <pre className="mono" style={{ whiteSpace: "pre-wrap", margin: 0, fontSize: 12 }}>
{`curl -H "X-OWEN-Key: ${issued.slice(0, 16)}..." \\
  https://api.owen.santiagoproperties.uk/api/ai/docs`}
              </pre>
            </div>

            <button style={{ marginTop: 12 }} onClick={onClose}>
              I've saved the key — close
            </button>
          </>
        ) : (
          <>
            <div style={{ marginTop: 12 }}>
              <label className="muted" style={{ display: "block", marginBottom: 4 }}>
                Name — who or what is this key for?
              </label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. claude-cli, reporting-app"
                style={{ width: "100%" }}
                autoFocus
              />
            </div>

            <div className="card" style={{ marginTop: 12 }}>
              <div className="l" style={{ marginBottom: 8 }}>Scopes</div>
              {Object.entries(SCOPE_HELP).map(([scope, help]) => (
                <label key={scope} style={{ display: "block", marginBottom: 10, cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={scopes.includes(scope)}
                    onChange={() => toggle(scope)}
                    style={{ marginRight: 8 }}
                  />
                  <b>{scope}</b>
                  <div className="muted" style={{ marginLeft: 24, fontSize: 12 }}>{help}</div>
                </label>
              ))}
              <p className="muted" style={{ margin: 0, fontSize: 12 }}>
                Every scope is read-only. No key can change OWEN's data.
              </p>
            </div>

            <div style={{ marginTop: 12 }}>
              <label className="muted" style={{ display: "block", marginBottom: 4 }}>
                Expires after (days) — optional
              </label>
              <input
                value={expires}
                onChange={(e) => setExpires(e.target.value.replace(/\D/g, ""))}
                placeholder="leave empty for no expiry"
                style={{ width: "100%" }}
              />
            </div>

            {create.isError && (
              <p style={{ color: "var(--danger)" }}>{String((create.error as any)?.message || create.error)}</p>
            )}

            <button
              style={{ marginTop: 16 }}
              disabled={!name.trim() || scopes.length === 0 || create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "Create key"}
            </button>
          </>
        )}
      </div>
    </>
  );
}

function UsagePanel({ id }: { id: string }) {
  const { data } = useQuery({ queryKey: ["api-key-usage", id], queryFn: () => api.apiKeyUsage(id) });
  if (!data) return <div className="muted">Loading…</div>;
  const items = (data as any).items || [];
  if (!items.length) return <div className="muted">No requests recorded yet.</div>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr><th>When</th><th>Endpoint</th><th>Status</th><th>ms</th><th>Rows</th><th>SQL / error</th></tr>
        </thead>
        <tbody>
          {items.map((u: any, i: number) => (
            <tr key={i}>
              <td className="muted">{u.at ? new Date(u.at).toLocaleString() : "—"}</td>
              <td className="mono">{u.endpoint}</td>
              <td className={u.status_code >= 400 ? "" : "muted"}>
                {u.status_code >= 400
                  ? <span className="badge spam">{u.status_code}</span>
                  : u.status_code}
              </td>
              <td className="muted">{u.duration_ms}</td>
              <td className="muted">{u.rows ?? "—"}</td>
              <td className="mono" style={{ maxWidth: 320, fontSize: 11, wordBreak: "break-all" }}>
                {u.error || u.sql || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ApiKeys() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["api-keys"], queryFn: api.apiKeys });
  const [creating, setCreating] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeApiKey(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-keys"] }),
  });

  if (!data) return <div>Loading…</div>;
  const items = (data as any).items || [];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
        <h2 style={{ marginTop: 0 }}>API Keys</h2>
        <button onClick={() => setCreating(true)}>New key</button>
      </div>
      <p className="muted">
        Read-only credentials for the AI API (<code className="mono">/api/ai/*</code>). Give an AI
        agent or an outside integration the base URL and a key; it can learn the whole API from{" "}
        <code className="mono">GET /api/ai/docs</code>. No key can change anything in OWEN.
      </p>

      {!items.length && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            No keys yet. Create one to let an AI query calls, leads, spend and errors.
          </p>
        </div>
      )}

      {items.length > 0 && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Key</th><th>Scopes</th><th>State</th>
                <th>Last used</th><th>24h</th><th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((k: any) => (
                <Fragment key={k.id}>
                  <tr>
                    <td><b>{k.name}</b></td>
                    <td className="mono muted">{k.key_prefix}…</td>
                    <td><ScopeBadges scopes={k.scopes} /></td>
                    <td>
                      {k.revoked_at ? <span className="badge spam">revoked</span>
                        : k.expired ? <span className="badge spam">expired</span>
                        : <span className="badge new">active</span>}
                    </td>
                    <td className="muted">
                      {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}
                    </td>
                    <td className="muted">{k.requests_24h}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <button onClick={() => setExpanded(expanded === k.id ? null : k.id)}>
                        {expanded === k.id ? "Hide" : "Usage"}
                      </button>{" "}
                      {!k.revoked_at && (
                        <button
                          onClick={() => {
                            if (confirm(`Revoke "${k.name}"? Anything using this key stops working immediately.`))
                              revoke.mutate(k.id);
                          }}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                  {expanded === k.id && (
                    <tr>
                      <td colSpan={7} style={{ background: "var(--panel2)" }}>
                        <UsagePanel id={k.id} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card" style={{ marginTop: 12 }}>
        <div className="l" style={{ marginBottom: 8 }}>Scopes</div>
        <div className="kv">
          {Object.entries(SCOPE_HELP).map(([scope, help]) => (
            <Fragment key={scope}>
              <span className="muted">{scope}</span>
              <span>{help}</span>
            </Fragment>
          ))}
        </div>
        <p className="muted" style={{ marginBottom: 0, fontSize: 12 }}>
          Revoking keeps the key's request history — the row stays so past usage remains
          attributable.
        </p>
      </div>

      {creating && <CreateModal onClose={() => setCreating(false)} />}
    </div>
  );
}
