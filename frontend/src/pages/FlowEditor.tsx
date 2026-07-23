import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import {
  MENU_DIGITS,
  type FlowForm,
  type MenuOption,
  buildGraph,
  emptyForm,
  parseGraph,
} from "../lib/flowGraph";

type FlowVersion = { id: string; version: number; graph: any; created_at?: string | null };
type FlowDetail = {
  id: string;
  name: string;
  active_version_id?: string | null;
  versions: FlowVersion[];
};

type Feedback = { kind: "errors" | "warnings" | "ok"; errors: string[]; warnings: string[] } | null;

// Rule-form flow authoring (Ticket 08). The operator fills a linear 5-section form; on save
// it is serialized (lib/flowGraph.buildGraph) into a Ticket-02 graph and appended as a new
// immutable flow version. "Save draft" never validates; "Activate" saves then runs the
// backend activation checks, surfacing hard errors (block) vs warnings (allow). The visual
// builder is a disabled "later" tab. Reached from the Call Flows library (new + open).
export default function FlowEditor() {
  const { id } = useParams();
  const qc = useQueryClient();
  const { data } = useQuery<FlowDetail>({ queryKey: ["flow", id], queryFn: () => api.flow(id!) });

  const [form, setForm] = useState<FlowForm>(emptyForm());
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState<"form" | "visual">("form");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);

  // Load the latest version's graph into the form once (round-trip if form-authored).
  useEffect(() => {
    if (!data || loaded) return;
    const latest = data.versions[data.versions.length - 1];
    if (latest) {
      const parsed = parseGraph(latest.graph);
      if (parsed) setForm(parsed);
    }
    setLoaded(true);
  }, [data, loaded]);

  const graph = useMemo(() => buildGraph(form), [form]);
  const set = <K extends keyof FlowForm>(k: K, v: FlowForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  async function saveDraft(): Promise<string | null> {
    setBusy(true);
    setFeedback(null);
    try {
      const v = await api.saveFlowVersion(id!, graph);
      setSavedVersion(v.version);
      qc.invalidateQueries({ queryKey: ["flow", id] });
      qc.invalidateQueries({ queryKey: ["flows"] });
      return v.id;
    } catch (e: any) {
      setFeedback({ kind: "errors", errors: [`Save failed: ${e?.message || e}`], warnings: [] });
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    setBusy(true);
    setFeedback(null);
    let versionId: string;
    try {
      const v = await api.saveFlowVersion(id!, graph);
      setSavedVersion(v.version);
      versionId = v.id;
    } catch (e: any) {
      setBusy(false);
      setFeedback({ kind: "errors", errors: [`Save failed: ${e?.message || e}`], warnings: [] });
      return;
    }
    try {
      const res = await api.activateFlowVersion(id!, versionId);
      setFeedback({ kind: "ok", errors: [], warnings: res.warnings || [] });
      qc.invalidateQueries({ queryKey: ["flow", id] });
      qc.invalidateQueries({ queryKey: ["flows"] });
    } catch (e: any) {
      // The flows API refuses hard-error activation with HTTP 400 whose body is
      // {"detail": {errors, warnings}} — ApiError.message carries that raw JSON text.
      let errors = [String(e?.message || e)];
      let warnings: string[] = [];
      if (e instanceof ApiError) {
        try {
          const detail = JSON.parse(e.message)?.detail;
          if (detail && Array.isArray(detail.errors)) {
            errors = detail.errors;
            warnings = detail.warnings || [];
          }
        } catch {
          /* leave raw message */
        }
      }
      setFeedback({ kind: "errors", errors, warnings });
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className="muted">Loading…</div>;

  return (
    <div>
      <Link to="/flows">← Call Flows</Link>
      <div className="toolbar" style={{ marginTop: 10 }}>
        <h2 style={{ margin: 0, flex: 1 }}>{data.name}</h2>
        {data.active_version_id ? (
          <span className="badge new">active</span>
        ) : (
          <span className="badge">draft</span>
        )}
      </div>

      <div className="tabs">
        <button className={"tab" + (tab === "form" ? " active" : "")} onClick={() => setTab("form")}>
          Rule form
        </button>
        <button className="tab" disabled title="The visual flow builder arrives in a later ticket">
          Visual builder (coming soon)
        </button>
      </div>

      {tab === "form" && (
        <>
          {/* (1) Business hours */}
          <Section title="1 · Business hours" hint="Route calls differently in and out of hours.">
            <label className="chk">
              <input
                type="checkbox"
                checked={form.hoursEnabled}
                onChange={(e) => set("hoursEnabled", e.target.checked)}
              />{" "}
              Apply a business-hours schedule
            </label>
            {form.hoursEnabled && (
              <div style={{ marginTop: 10 }}>
                <Field label="Open schedule">
                  <input
                    style={{ width: "100%" }}
                    placeholder="e.g. Mon–Fri 9:00–17:00"
                    value={form.hoursSchedule}
                    onChange={(e) => set("hoursSchedule", e.target.value)}
                  />
                </Field>
                <p className="muted" style={{ margin: "6px 0 0" }}>
                  During open hours the greeting plays; outside them callers go straight to the
                  fallback voicemail.
                </p>
              </div>
            )}
          </Section>

          {/* (2) Greeting + record */}
          <Section title="2 · Greeting" hint="What callers hear first.">
            <Field label="Greeting">
              <textarea
                style={{ width: "100%", minHeight: 60 }}
                value={form.greeting}
                onChange={(e) => set("greeting", e.target.value)}
              />
            </Field>
            <label className="chk" style={{ marginTop: 8 }}>
              <input
                type="checkbox"
                checked={form.record}
                onChange={(e) => set("record", e.target.checked)}
              />{" "}
              Record this call
            </label>
            {form.record && (
              <div style={{ marginTop: 8 }}>
                <Field label="Consent notice">
                  <input
                    style={{ width: "100%" }}
                    placeholder="e.g. This call may be recorded for quality."
                    value={form.consentNotice}
                    onChange={(e) => set("consentNotice", e.target.value)}
                  />
                </Field>
              </div>
            )}
          </Section>

          {/* (3) IVR menu (optional) */}
          <Section title="3 · Menu (optional)" hint="Offer callers digit options.">
            <label className="chk">
              <input
                type="checkbox"
                checked={form.menuEnabled}
                onChange={(e) => set("menuEnabled", e.target.checked)}
              />{" "}
              Present a keypad menu
            </label>
            {form.menuEnabled && (
              <div style={{ marginTop: 10 }}>
                <Field label="Menu prompt">
                  <input
                    style={{ width: "100%" }}
                    placeholder="e.g. Press 1 for sales, 2 for support."
                    value={form.menuPrompt}
                    onChange={(e) => set("menuPrompt", e.target.value)}
                  />
                </Field>
                <table style={{ marginTop: 10 }}>
                  <thead>
                    <tr>
                      <th style={{ width: 70 }}>Digit</th>
                      <th style={{ width: 130 }}>Destination</th>
                      <th>Number / label</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {form.menuOptions.map((opt, i) => (
                      <MenuRow
                        key={i}
                        opt={opt}
                        onChange={(next) =>
                          set(
                            "menuOptions",
                            form.menuOptions.map((o, j) => (j === i ? next : o))
                          )
                        }
                        onRemove={() =>
                          set(
                            "menuOptions",
                            form.menuOptions.filter((_, j) => j !== i)
                          )
                        }
                      />
                    ))}
                    {form.menuOptions.length === 0 && (
                      <tr>
                        <td colSpan={4} className="muted">
                          No options yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
                <button
                  style={{ marginTop: 8 }}
                  onClick={() =>
                    set("menuOptions", [
                      ...form.menuOptions,
                      { digit: nextFreeDigit(form.menuOptions), kind: "dial", number: "", label: "" },
                    ])
                  }
                >
                  + Add option
                </button>
              </div>
            )}
          </Section>

          {/* (4) Default routing */}
          <Section
            title="4 · Default routing"
            hint="Where the greeting leads when there is no menu (or on menu timeout)."
          >
            <Field label="On answer">
              <select
                value={form.defaultRouting}
                onChange={(e) => set("defaultRouting", e.target.value as FlowForm["defaultRouting"])}
              >
                <option value="voicemail">Send to voicemail</option>
                <option value="dial">Dial a number</option>
              </select>
            </Field>
            {form.defaultRouting === "dial" && (
              <div style={{ marginTop: 8 }}>
                <Field label="Dial number">
                  <input
                    style={{ width: "100%" }}
                    placeholder="+15555550123"
                    value={form.defaultDialNumber}
                    onChange={(e) => set("defaultDialNumber", e.target.value)}
                  />
                </Field>
              </div>
            )}
          </Section>

          {/* (5) Fallback */}
          <Section title="5 · Fallback" hint="The flow-level catch-all when nothing else answers.">
            <Field label="Voicemail prompt">
              <textarea
                style={{ width: "100%", minHeight: 50 }}
                value={form.fallbackPrompt}
                onChange={(e) => set("fallbackPrompt", e.target.value)}
              />
            </Field>
          </Section>

          {feedback && (
            <div className="card" style={{ marginBottom: 12 }}>
              {feedback.kind === "ok" && (
                <div style={{ color: "var(--good)", fontWeight: 600 }}>✓ Flow activated.</div>
              )}
              {feedback.errors.length > 0 && (
                <div style={{ marginBottom: feedback.warnings.length ? 10 : 0 }}>
                  <div style={{ color: "var(--danger)", fontWeight: 600, marginBottom: 4 }}>
                    Errors (must fix before activating)
                  </div>
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {feedback.errors.map((e, i) => (
                      <li key={i} style={{ color: "var(--danger)" }}>
                        {e}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {feedback.warnings.length > 0 && (
                <div>
                  <div style={{ color: "var(--warn)", fontWeight: 600, marginBottom: 4 }}>
                    Warnings (allowed)
                  </div>
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {feedback.warnings.map((w, i) => (
                      <li key={i} style={{ color: "var(--warn)" }}>
                        {w}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          <div className="toolbar">
            <button disabled={busy} onClick={saveDraft}>
              Save draft
            </button>
            <button className="primary" disabled={busy} onClick={activate}>
              Activate
            </button>
            {savedVersion != null && (
              <span className="muted">Saved as version {savedVersion}.</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: any }) {
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="l" style={{ marginBottom: hint ? 2 : 8 }}>
        {title}
      </div>
      {hint && (
        <p className="muted" style={{ margin: "0 0 10px" }}>
          {hint}
        </p>
      )}
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: any }) {
  return (
    <label style={{ display: "block", marginBottom: 4 }}>
      <span className="muted" style={{ display: "block", fontSize: 12, marginBottom: 4 }}>
        {label}
      </span>
      {children}
    </label>
  );
}

function MenuRow({
  opt,
  onChange,
  onRemove,
}: {
  opt: MenuOption;
  onChange: (o: MenuOption) => void;
  onRemove: () => void;
}) {
  return (
    <tr>
      <td>
        <select value={opt.digit} onChange={(e) => onChange({ ...opt, digit: e.target.value })}>
          {MENU_DIGITS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </td>
      <td>
        <select
          value={opt.kind}
          onChange={(e) => onChange({ ...opt, kind: e.target.value as MenuOption["kind"] })}
        >
          <option value="dial">Dial</option>
          <option value="voicemail">Voicemail</option>
        </select>
      </td>
      <td>
        {opt.kind === "dial" ? (
          <input
            style={{ width: "100%" }}
            placeholder="+15555550123"
            value={opt.number}
            onChange={(e) => onChange({ ...opt, number: e.target.value })}
          />
        ) : (
          <span className="muted">→ fallback voicemail</span>
        )}
      </td>
      <td style={{ textAlign: "right" }}>
        <button onClick={onRemove}>Remove</button>
      </td>
    </tr>
  );
}

function nextFreeDigit(opts: MenuOption[]): string {
  const used = new Set(opts.map((o) => o.digit));
  return MENU_DIGITS.find((d) => !used.has(d)) || "0";
}
