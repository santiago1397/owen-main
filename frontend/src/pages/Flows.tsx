import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

type Flow = {
  id: string;
  name: string;
  active_version_id?: string | null;
  created_at?: string | null;
  archived_at?: string | null;
  version_count?: number;
  attributed_call_count?: number;
};

// Call Flows library. Lists flows from the Ticket-02 flows API (one flow → many numbers).
// "New flow" creates a flow and opens the rule-form authoring view (Ticket 08); "Open"
// opens an existing flow's latest version in that same form.
//
// Library management (clone / rename / delete) lives HERE and only here: the list is where
// you browse and curate, the editor is where you edit one graph. Cloning deliberately
// copies the ACTIVE version — what actually answers calls — not whatever draft happens to
// be on someone's canvas, so a clone reproduces working behaviour.
export default function Flows() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [showArchived, setShowArchived] = useState(false);
  const { data } = useQuery<Flow[]>({
    queryKey: ["flows", showArchived],
    queryFn: () => (showArchived ? api.flowsIncludingArchived() : api.flows()),
  });
  const [name, setName] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["flows"] });

  const create = useMutation({
    mutationFn: (n: string) => api.createFlow(n),
    onSuccess: (flow: Flow) => {
      refresh();
      nav(`/flows/${flow.id}`);
    },
  });

  const newFlow = () => {
    const n = name.trim();
    if (!n || create.isPending) return;
    create.mutate(n);
  };

  // Pre-fill the clone name so accepting the default is one Enter, but never silently reuse
  // a name already on screen — flows have no unique constraint, so duplicates are legal and
  // therefore easy to create by accident.
  const suggestName = (base: string) => {
    const taken = new Set((data || []).map((f) => f.name));
    if (!taken.has(`${base} (copy)`)) return `${base} (copy)`;
    for (let i = 2; i < 100; i++) {
      if (!taken.has(`${base} (copy ${i})`)) return `${base} (copy ${i})`;
    }
    return `${base} (copy)`;
  };

  const clone = async (f: Flow) => {
    const proposed = window.prompt(`Name for the copy of "${f.name}":`, suggestName(f.name));
    if (proposed === null) return; // cancelled
    const n = proposed.trim();
    if (!n) return;
    setNote(null);
    try {
      const created: Flow = await api.cloneFlow(f.id, n);
      refresh();
      nav(`/flows/${created.id}`); // a clone exists to be edited — land in the editor
    } catch (e: any) {
      setNote(String(e?.message || e));
    }
  };

  const rename = async (f: Flow) => {
    const proposed = window.prompt(`Rename "${f.name}" to:`, f.name);
    if (proposed === null) return;
    const n = proposed.trim();
    if (!n || n === f.name) return;
    setNote(null);
    try {
      await api.renameFlow(f.id, n);
      refresh();
    } catch (e: any) {
      setNote(String(e?.message || e));
    }
  };

  // Delete is destructive for an unused flow and merely hides a used one, so the confirm
  // says WHICH before you commit — that's what attributed_call_count is on the list for.
  // (A flow still assigned to a number is refused by the backend with a 409 naming it.)
  const remove = async (f: Flow) => {
    const calls = f.attributed_call_count || 0;
    const message =
      calls > 0
        ? `Archive "${f.name}"?\n\n${calls} call${calls === 1 ? " is" : "s are"} attributed ` +
          `to this flow, so it can't be deleted without losing that history. It will be ` +
          `hidden from the library and can be restored.`
        : `Permanently delete "${f.name}"?\n\nNo calls are attributed to it, so it will be ` +
          `removed for good. This cannot be undone.`;
    if (!window.confirm(message)) return;
    setNote(null);
    try {
      const res = await api.deleteFlow(f.id);
      refresh();
      setNote(res?.outcome === "deleted" ? `Deleted "${f.name}".` : `Archived "${f.name}".`);
    } catch (e: any) {
      setNote(String(e?.message || e));
    }
  };

  const restore = async (f: Flow) => {
    setNote(null);
    try {
      await api.restoreFlow(f.id);
      refresh();
    } catch (e: any) {
      setNote(String(e?.message || e));
    }
  };

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ marginTop: 0, marginBottom: 0, flex: 1 }}>Call Flows</h2>
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          Show archived
        </label>
        <input
          placeholder="new flow name…"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && newFlow()}
        />
        <button className="primary" disabled={!name.trim() || create.isPending} onClick={newFlow}>
          New flow
        </button>
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        Reusable call-handling graphs. One flow can be assigned to many platform numbers.
        Clone one to start a new flow from a working graph instead of an empty canvas.
      </p>
      {note && (
        <div className="card" style={{ padding: 8, marginBottom: 8 }}>
          <span className="muted">{note}</span>
        </div>
      )}

      <div className="card">
        <div className="tablewrap"><table>
          <thead>
            <tr>
              <th>Name</th><th>Status</th><th>Versions</th><th>Calls</th>
              <th>Created</th><th></th>
            </tr>
          </thead>
          <tbody>
            {(data || []).map((f) => (
              <tr key={f.id} style={f.archived_at ? { opacity: 0.55 } : undefined}>
                <td>{f.name}</td>
                <td>
                  {f.archived_at
                    ? <span className="badge">archived</span>
                    : f.active_version_id
                      ? <span className="badge new">active</span>
                      : <span className="badge">draft</span>}
                </td>
                <td>{f.version_count ?? 0}</td>
                <td>{f.attributed_call_count ?? 0}</td>
                <td>{f.created_at ? new Date(f.created_at).toLocaleString() : "—"}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {f.archived_at ? (
                    <button onClick={() => restore(f)}>Restore</button>
                  ) : (
                    <>
                      {/* A flow with no saved version has nothing to copy — the backend 422s,
                          so don't offer an action that can only fail. */}
                      <button
                        onClick={() => clone(f)}
                        disabled={(f.version_count ?? 0) === 0}
                        title={(f.version_count ?? 0) === 0
                          ? "This flow has no saved version to clone"
                          : "Copy this flow's active graph into a new flow"}
                      >
                        Clone
                      </button>{" "}
                      <button onClick={() => rename(f)}>Rename</button>{" "}
                      <button onClick={() => remove(f)}>
                        {(f.attributed_call_count ?? 0) > 0 ? "Archive" : "Delete"}
                      </button>{" "}
                    </>
                  )}
                  <button onClick={() => nav(`/flows/${f.id}`)}>Open</button>
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr><td colSpan={6} className="muted" style={{ textAlign: "center", padding: 20 }}>
                No flows yet.
              </td></tr>
            )}
          </tbody>
        </table></div>
      </div>
    </div>
  );
}
