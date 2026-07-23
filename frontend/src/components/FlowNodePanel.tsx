import {
  CONDITION_OPERATORS,
  DAYS,
  MENU_DIGITS,
  NODE_TITLES,
  type CanvasNode,
  type ConditionOperator,
  type ConditionRow,
  type NodeConfig,
  conditionRows,
  rowPortName,
} from "../lib/canvasGraph";

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

      {ntype === "set_vars" && <SetVarsEditor config={config} set={set} />}

      {ntype === "unset_vars" && <UnsetVarsEditor config={config} set={set} />}

      {ntype === "conditions" && <ConditionsEditor config={config} set={set} />}

      {ntype === "send_sms" && (
        <>
          <Field label="To (blank = the caller)">
            <input
              style={{ width: "100%" }}
              placeholder="{{caller_number}}"
              value={config.to || ""}
              onChange={(e) => set("to", e.target.value)}
            />
          </Field>
          <Field label="Message body">
            <textarea
              style={{ width: "100%", minHeight: 70 }}
              placeholder="Thanks for calling! …"
              value={config.body || ""}
              onChange={(e) => set("body", e.target.value)}
            />
          </Field>
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
            Sent fire-and-forget from this flow's DID. Use{" "}
            <code className="mono">{"{{caller_number}}"}</code> and other variables in either
            field. Opt-out and 10DLC gates apply; the call never waits on delivery.
          </p>
        </>
      )}

      {ntype === "request" && <RequestEditor config={config} set={set} />}

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

// --- Ticket 17: key/value rows shared by set_vars, request headers ------------------------

function KeyValueRows({
  pairs,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
}: {
  pairs: [string, string][];
  onChange: (pairs: [string, string][]) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
}) {
  const setPair = (i: number, key: string, value: string) =>
    onChange(pairs.map((p, j) => (j === i ? [key, value] : p)));
  return (
    <div>
      {pairs.map(([k, v], i) => (
        <div key={i} className="kvrow" style={{ display: "flex", gap: 6, marginBottom: 6 }}>
          <input
            style={{ flex: "0 0 40%" }}
            placeholder={keyPlaceholder}
            value={k}
            onChange={(e) => setPair(i, e.target.value, v)}
          />
          <input
            style={{ flex: 1 }}
            placeholder={valuePlaceholder}
            value={v}
            onChange={(e) => setPair(i, k, e.target.value)}
          />
          <button type="button" onClick={() => onChange(pairs.filter((_, j) => j !== i))}>
            ✕
          </button>
        </div>
      ))}
      <button type="button" className="hoursadd" onClick={() => onChange([...pairs, ["", ""]])}>
        + row
      </button>
    </div>
  );
}

// set_vars config: {vars: {name: "literal or {{var}}"}}. Edited as ordered key/value rows;
// serialized back to an object (a stable-order object, matching the interpreter's iteration).
function SetVarsEditor({ config, set }: { config: NodeConfig; set: (k: string, v: any) => void }) {
  const vars: Record<string, string> =
    config.vars && typeof config.vars === "object" ? config.vars : {};
  const pairs = Object.entries(vars).map(([k, v]) => [k, String(v)] as [string, string]);
  const commit = (next: [string, string][]) => {
    const obj: Record<string, string> = {};
    for (const [k, v] of next) if (k.trim()) obj[k.trim()] = v;
    set("vars", next.length ? obj : undefined);
  };
  return (
    <Field label="Variables to set (value may contain {{vars}})">
      <KeyValueRows
        pairs={pairs.length ? pairs : [["", ""]]}
        onChange={commit}
        keyPlaceholder="name"
        valuePlaceholder="literal or {{var}}"
      />
    </Field>
  );
}

// unset_vars config: {names: [...]}. One name per row.
function UnsetVarsEditor({ config, set }: { config: NodeConfig; set: (k: string, v: any) => void }) {
  const names: string[] = Array.isArray(config.names) ? config.names : [];
  const commit = (next: string[]) => set("names", next.some((n) => n.trim()) ? next.map((n) => n.trim()).filter(Boolean) : undefined);
  const rows = names.length ? names : [""];
  return (
    <Field label="Variable names to remove">
      <div>
        {rows.map((n, i) => (
          <div key={i} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
            <input
              style={{ flex: 1 }}
              placeholder="name"
              value={n}
              onChange={(e) => commit(rows.map((x, j) => (j === i ? e.target.value : x)))}
            />
            <button type="button" onClick={() => commit(rows.filter((_, j) => j !== i))}>
              ✕
            </button>
          </div>
        ))}
        <button type="button" className="hoursadd" onClick={() => commit([...rows, ""])}>
          + name
        </button>
      </div>
    </Field>
  );
}

// conditions config: {rows: [{variable, operator, value, port}]}. Ordered editor; first
// match wins at runtime. Each row's port is auto-derived (match_1, match_2, …) and kept
// stable per position; the always-present "else" port catches no-match. Up/down reorder.
function ConditionsEditor({ config, set }: { config: NodeConfig; set: (k: string, v: any) => void }) {
  const rows = conditionRows(config);
  // Re-derive positional port names so ports stay match_<n> after add/remove/reorder.
  const commit = (next: ConditionRow[]) =>
    set(
      "rows",
      next.length
        ? next.map((r, i) => ({ ...r, port: rowPortName(i) }))
        : undefined
    );
  const setRow = (i: number, patch: Partial<ConditionRow>) =>
    commit(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= rows.length) return;
    const next = [...rows];
    [next[i], next[j]] = [next[j], next[i]];
    commit(next);
  };
  const addRow = () =>
    commit([...rows, { variable: "", operator: "equals", value: "", port: rowPortName(rows.length) }]);

  return (
    <>
      <Field label="Rules (first match wins; each adds an output port)">
        <div>
          {rows.map((r, i) => (
            <div key={i} className="condrow" style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 8, marginBottom: 8 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                <span className="badge">{rowPortName(i)}</span>
                <span style={{ display: "flex", gap: 4 }}>
                  <button type="button" disabled={i === 0} onClick={() => move(i, -1)} title="Move up">
                    ↑
                  </button>
                  <button type="button" disabled={i === rows.length - 1} onClick={() => move(i, 1)} title="Move down">
                    ↓
                  </button>
                  <button type="button" onClick={() => commit(rows.filter((_, j) => j !== i))} title="Remove rule">
                    ✕
                  </button>
                </span>
              </div>
              <input
                style={{ width: "100%", marginBottom: 6 }}
                placeholder="variable (e.g. gather.digits, request.body.status)"
                value={r.variable || ""}
                onChange={(e) => setRow(i, { variable: e.target.value })}
              />
              <div style={{ display: "flex", gap: 6 }}>
                <select
                  value={r.operator || "equals"}
                  onChange={(e) => setRow(i, { operator: e.target.value as ConditionOperator })}
                >
                  {CONDITION_OPERATORS.map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </select>
                {r.operator !== "is_empty" && (
                  <input
                    style={{ flex: 1 }}
                    placeholder="value (may contain {{var}})"
                    value={r.value || ""}
                    onChange={(e) => setRow(i, { value: e.target.value })}
                  />
                )}
              </div>
            </div>
          ))}
          <button type="button" className="hoursadd" onClick={addRow}>
            + rule
          </button>
        </div>
      </Field>
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
        Rows evaluate top-to-bottom; the first match takes its port. If nothing matches, the{" "}
        <code className="mono">else</code> port is taken. <code className="mono">gt</code>/
        <code className="mono">lt</code> compare numerically when both sides are numbers.
      </p>
    </>
  );
}

// request config: {method, url, headers?, body?}. Method select, url, header key/value rows,
// JSON/text body. All fields interpolate {{vars}} at call time.
function RequestEditor({ config, set }: { config: NodeConfig; set: (k: string, v: any) => void }) {
  const headers: Record<string, string> =
    config.headers && typeof config.headers === "object" ? config.headers : {};
  const headerPairs = Object.entries(headers).map(([k, v]) => [k, String(v)] as [string, string]);
  const commitHeaders = (next: [string, string][]) => {
    const obj: Record<string, string> = {};
    for (const [k, v] of next) if (k.trim()) obj[k.trim()] = v;
    set("headers", Object.keys(obj).length ? obj : undefined);
  };
  return (
    <>
      <div style={{ display: "flex", gap: 8 }}>
        <Field label="Method">
          <select value={config.method || "GET"} onChange={(e) => set("method", e.target.value)}>
            <option value="GET">GET</option>
            <option value="POST">POST</option>
          </select>
        </Field>
        <Field label="URL">
          <input
            style={{ width: "100%", minWidth: 200 }}
            placeholder="https://api.example.com/lookup?n={{caller_number}}"
            value={config.url || ""}
            onChange={(e) => set("url", e.target.value)}
          />
        </Field>
      </div>
      <Field label="Headers">
        <KeyValueRows
          pairs={headerPairs.length ? headerPairs : [["", ""]]}
          onChange={commitHeaders}
          keyPlaceholder="Header-Name"
          valuePlaceholder="value or {{var}}"
        />
      </Field>
      {(config.method || "GET") === "POST" && (
        <Field label="Body (JSON or text; {{vars}} interpolated)">
          <textarea
            style={{ width: "100%", minHeight: 70 }}
            placeholder={'{"caller": "{{caller_number}}"}'}
            value={config.body || ""}
            onChange={(e) => set("body", e.target.value)}
          />
        </Field>
      )}
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
        5s timeout. A 2xx response takes the <code className="mono">success</code> port and
        exposes <code className="mono">{"{{request.status}}"}</code> /{" "}
        <code className="mono">{"{{request.body.*}}"}</code>; anything else takes{" "}
        <code className="mono">failure</code>.
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
