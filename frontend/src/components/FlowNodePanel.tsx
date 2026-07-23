import { DAYS, MENU_DIGITS, NODE_TITLES, type CanvasNode, type NodeConfig } from "../lib/canvasGraph";

// Per-type node config side panel (Ticket 16). Pure controlled component: the editor owns
// the node state; every input funnels through onChange(partial-config). `record` +
// `consent_notice` are exposed here as a checkbox/textarea on the play panel (a node
// MODIFIER, never its own node type — mirrors the backend validator).

type Agent = { id: string; name: string; active_version_id?: string | null };

export default function FlowNodePanel({
  node,
  agents,
  readOnly,
  onChange,
  onDelete,
}: {
  node: CanvasNode;
  agents: Agent[];
  readOnly: boolean;
  onChange: (patch: NodeConfig) => void;
  onDelete: () => void;
}) {
  const { ntype, config } = node.data;
  const set = (k: string, v: any) => onChange({ [k]: v });

  return (
    <fieldset disabled={readOnly} style={{ border: "none", margin: 0, padding: 0 }}>
      <div className="l" style={{ marginBottom: 2 }}>
        {NODE_TITLES[ntype]} node
      </div>
      <p className="muted" style={{ margin: "0 0 10px", fontSize: 12 }}>
        id: <code className="mono">{node.id}</code>
      </p>

      <Field label="Label (operator-facing)">
        <input
          style={{ width: "100%" }}
          value={config.label || ""}
          placeholder="e.g. Main greeting"
          onChange={(e) => set("label", e.target.value)}
        />
      </Field>

      {ntype === "play" && (
        <>
          <Field label="Prompt (spoken via TTS)">
            <textarea
              style={{ width: "100%", minHeight: 70 }}
              value={config.prompt || ""}
              onChange={(e) => set("prompt", e.target.value)}
            />
          </Field>
          <label className="chk" style={{ display: "block", marginTop: 8 }}>
            <input
              type="checkbox"
              checked={!!config.record}
              onChange={(e) => set("record", e.target.checked || undefined)}
            />{" "}
            Record the call from this point
          </label>
          {config.record && (
            <div style={{ marginTop: 8 }}>
              <Field label="Consent notice">
                <textarea
                  style={{ width: "100%", minHeight: 44 }}
                  placeholder="e.g. This call may be recorded for quality."
                  value={config.consent_notice || ""}
                  onChange={(e) => set("consent_notice", e.target.value)}
                />
              </Field>
            </div>
          )}
        </>
      )}

      {ntype === "hours" && <HoursEditor config={config} set={set} />}

      {ntype === "menu" && (
        <>
          <Field label="Menu prompt">
            <textarea
              style={{ width: "100%", minHeight: 56 }}
              placeholder="e.g. Press 1 for sales, 2 for support."
              value={config.prompt || ""}
              onChange={(e) => set("prompt", e.target.value)}
            />
          </Field>
          <div style={{ display: "flex", gap: 8 }}>
            <Field label="Timeout (s)">
              <input
                type="number"
                min={1}
                style={{ width: 90 }}
                value={config.timeout ?? 5}
                onChange={(e) => set("timeout", numOr(e.target.value, 5))}
              />
            </Field>
            <Field label="Max digits">
              <input
                type="number"
                min={1}
                max={10}
                style={{ width: 90 }}
                value={config.max_digits ?? 1}
                onChange={(e) => set("max_digits", numOr(e.target.value, 1))}
              />
            </Field>
          </div>
          <Field label="Enabled digits (each adds an output port)">
            <div className="digitgrid">
              {MENU_DIGITS.map((d) => {
                const on = (config.digits || []).includes(d);
                return (
                  <button
                    key={d}
                    type="button"
                    className={"digit" + (on ? " on" : "")}
                    onClick={() => {
                      const cur: string[] = config.digits || [];
                      set(
                        "digits",
                        on ? cur.filter((x) => x !== d) : MENU_DIGITS.filter((x) => cur.includes(x) || x === d)
                      );
                    }}
                  >
                    {d}
                  </button>
                );
              })}
            </div>
          </Field>
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
            Unwired digits and the timeout/invalid ports fall through to the flow's default
            fallback at runtime.
          </p>
        </>
      )}

      {ntype === "dial" && (
        <>
          <Field label="Target kind">
            <select
              value={config.target_kind || "number"}
              onChange={(e) => set("target_kind", e.target.value)}
            >
              <option value="number">Phone number</option>
              <option value="operator">Operator (softphone)</option>
            </select>
          </Field>
          {(config.target_kind || "number") === "operator" ? (
            <Field label="Operator ids (comma-separated; first to answer wins)">
              <input
                style={{ width: "100%" }}
                placeholder="e.g. owen"
                value={(config.operators || []).join(", ")}
                onChange={(e) =>
                  set(
                    "operators",
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
              />
            </Field>
          ) : (
            <Field label="Target number">
              <input
                style={{ width: "100%" }}
                placeholder="+15555550123"
                value={config.target || config.number || ""}
                onChange={(e) => set("target", e.target.value)}
              />
            </Field>
          )}
          <Field label="Caller ID override (blank = passthrough of the caller's number)">
            <input
              style={{ width: "100%" }}
              placeholder="+15555550123"
              value={config.caller_id || ""}
              onChange={(e) => set("caller_id", e.target.value)}
            />
          </Field>
          <Field label="Ring timeout (s)">
            <input
              type="number"
              min={5}
              style={{ width: 90 }}
              value={config.timeout ?? 25}
              onChange={(e) => set("timeout", numOr(e.target.value, 25))}
            />
          </Field>
        </>
      )}

      {ntype === "voicemail" && (
        <Field label="Voicemail greeting">
          <textarea
            style={{ width: "100%", minHeight: 70 }}
            placeholder="e.g. Sorry we missed you — please leave a message."
            value={config.greeting || config.prompt || ""}
            onChange={(e) => set("greeting", e.target.value)}
          />
        </Field>
      )}

      {ntype === "ai_agent" && (
        <>
          <Field label="Agent">
            <select
              value={config.agent_id || ""}
              onChange={(e) => {
                const a = agents.find((x) => x.id === e.target.value);
                onChange({ agent_id: e.target.value || undefined, agent_name: a?.name });
              }}
            >
              <option value="">select an agent…</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                  {a.active_version_id ? "" : " (no active version)"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Greeting (spoken before the agent takes over)">
            <textarea
              style={{ width: "100%", minHeight: 56 }}
              value={config.greeting || ""}
              onChange={(e) => set("greeting", e.target.value)}
            />
          </Field>
        </>
      )}

      {ntype === "hangup" && (
        <p className="muted" style={{ margin: 0 }}>
          Terminal node — hangs up the call. Nothing to configure.
        </p>
      )}
      {ntype === "entry" && (
        <p className="muted" style={{ margin: 0 }}>
          Every flow starts here. Wire its <code className="mono">default</code> port to the
          first real node. This node cannot be deleted.
        </p>
      )}

      {ntype !== "entry" && !readOnly && (
        <div style={{ marginTop: 14 }}>
          <button onClick={onDelete}>Delete node</button>
        </div>
      )}
    </fieldset>
  );
}

// --- hours schedule editor ({tz, schedule: {mon: [["09:00","17:00"]], ...}}) --------------

function HoursEditor({ config, set }: { config: NodeConfig; set: (k: string, v: any) => void }) {
  const hours = config.hours || {};
  const schedule: Record<string, [string, string][]> = hours.schedule || {};
  const setHours = (patch: any) => set("hours", { ...hours, ...patch });
  const setDay = (day: string, ranges: [string, string][]) => {
    const next = { ...schedule };
    if (ranges.length === 0) delete next[day];
    else next[day] = ranges;
    setHours({ schedule: next });
  };

  return (
    <>
      <Field label="Timezone (IANA; blank = business default)">
        <input
          style={{ width: "100%" }}
          placeholder="America/New_York"
          value={hours.tz || ""}
          onChange={(e) => setHours({ tz: e.target.value || undefined })}
        />
      </Field>
      <Field label="Weekly schedule (open windows; empty day = closed)">
        <div>
          {DAYS.map((day) => {
            const ranges = schedule[day] || [];
            return (
              <div key={day} className="hoursday">
                <span className="hoursdayname">{day}</span>
                <div style={{ flex: 1 }}>
                  {ranges.map((r, i) => (
                    <div key={i} className="hoursrange">
                      <input
                        type="time"
                        value={r[0]}
                        onChange={(e) =>
                          setDay(day, ranges.map((x, j) => (j === i ? [e.target.value, x[1]] : x)))
                        }
                      />
                      <span className="muted">–</span>
                      <input
                        type="time"
                        value={r[1]}
                        onChange={(e) =>
                          setDay(day, ranges.map((x, j) => (j === i ? [x[0], e.target.value] : x)))
                        }
                      />
                      <button type="button" onClick={() => setDay(day, ranges.filter((_, j) => j !== i))}>
                        ✕
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="hoursadd"
                    onClick={() => setDay(day, [...ranges, ["09:00", "17:00"]])}
                  >
                    + window
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </Field>
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
        No schedule at all = always open (fail-open, matching the runtime).
      </p>
    </>
  );
}

function Field({ label, children }: { label: string; children: any }) {
  return (
    <label style={{ display: "block", marginBottom: 10 }}>
      <span className="muted" style={{ display: "block", fontSize: 12, marginBottom: 4 }}>
        {label}
      </span>
      {children}
    </label>
  );
}

function numOr(v: string, dflt: number): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : dflt;
}
