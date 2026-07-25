import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../api";
import DateRangeBar from "../components/DateRangeBar";
import { PRESETS, type Range } from "../lib/dates";
import {
  type AttributedJob, type CampaignRow, type WorkizJob, UNATTRIBUTED,
  attribute, money0, parseWorkizCsv, pct, rollUp,
} from "../lib/workizJobs";

const COLORS = ["#4f8cff", "#37d67a", "#ffb020", "#ff5c6c", "#a78bfa", "#22d3ee"];
const AXIS = { stroke: "#9aa4b2", fontSize: 11 };
const TIP = { background: "#171a21", border: "1px solid #2a2f3a" };

// The uploaded export lives here and nowhere else. This module is read-only against the
// OWEN database, so job + revenue data is deliberately never persisted server-side.
const STORE_KEY = "attribution_jobs_v1";

type Stored = { uploadedAt: string; fileName: string; jobs: WorkizJob[] };

// Dates survive localStorage as ISO strings; rehydrate them or every date comparison
// silently becomes a string comparison.
function loadJobs(): Stored | null {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Stored;
    s.jobs = s.jobs.map((j) => ({
      ...j,
      createdAt: j.createdAt ? new Date(j.createdAt) : null,
      scheduledAt: j.scheduledAt ? new Date(j.scheduledAt) : null,
    }));
    return s;
  } catch {
    return null;
  }
}

function Stat({ n, l, hint }: { n: any; l: string; hint?: string }) {
  return (
    <div className="card stat">
      <div className="n">{n}</div>
      <div className="l">{l}</div>
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

export default function Attribution() {
  const [range, setRange] = useState<Range | null>(null);
  const [minDuration, setMinDuration] = useState(30);
  const [includeAhs, setIncludeAhs] = useState(false);
  const [stored, setStored] = useState<Stored | null>(loadJobs);
  const [parseErrors, setParseErrors] = useState<string[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const params = {
    date_from: range?.from.toISOString(),
    date_to: range?.to.toISOString(),
    min_duration_seconds: minDuration,
  };

  const calls = useQuery({
    queryKey: ["attribution-campaigns", params.date_from, params.date_to, minDuration],
    queryFn: () => api.attributionCampaigns(params),
    enabled: !!range,
  });

  // Only jobs inside the selected window are counted, so the calls side and the jobs side
  // always describe the same period. A job with no parseable created date is kept — dropping
  // it would understate revenue with no way for the operator to notice.
  const jobsInRange = useMemo(() => {
    if (!stored || !range) return [];
    return stored.jobs.filter(
      (j) => !j.createdAt || (j.createdAt >= range.from && j.createdAt < range.to),
    );
  }, [stored, range]);

  const phones = useMemo(
    () => Array.from(new Set(jobsInRange.map((j) => j.phone).filter(Boolean) as string[])),
    [jobsInRange],
  );

  const matches = useQuery({
    queryKey: ["attribution-match", phones.join(","), params.date_from, params.date_to, minDuration],
    queryFn: () => api.attributionMatch({ phones, ...params }),
    enabled: !!range && phones.length > 0,
  });

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    file.text().then((text) => {
      const { jobs, errors } = parseWorkizCsv(text);
      setParseErrors(errors);
      if (!jobs.length) return;
      const next: Stored = { uploadedAt: new Date().toISOString(), fileName: file.name, jobs };
      localStorage.setItem(STORE_KEY, JSON.stringify(next));
      setStored(next);
    });
    e.target.value = "";
  }

  function clearJobs() {
    localStorage.removeItem(STORE_KEY);
    setStored(null);
    setParseErrors([]);
  }

  const attributed: AttributedJob[] = useMemo(() => {
    const scoped = includeAhs ? jobsInRange : jobsInRange.filter((j) => !j.isAhs);
    return attribute(scoped, matches.data?.matches ?? []);
  }, [jobsInRange, includeAhs, matches.data]);

  const rows: CampaignRow[] = useMemo(
    () => rollUp(attributed, calls.data?.campaigns ?? []),
    [attributed, calls.data],
  );

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0, flex: 1 }}>Campaign performance</h2>
        <DateRangeBar defaultPreset="12m" presets={PRESETS} onChange={setRange} />
      </div>

      <div className="toolbar" style={{ gap: 16, flexWrap: "wrap" }}>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
          Qualified call is longer than
          <input
            type="number" min={0} max={600} value={minDuration}
            onChange={(e) => setMinDuration(Math.max(0, parseInt(e.target.value, 10) || 0))}
            style={{ width: 64 }}
          />
          seconds
        </label>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
          <input type="checkbox" checked={includeAhs} onChange={(e) => setIncludeAhs(e.target.checked)} />
          Include AHS jobs
        </label>
        <div style={{ flex: 1 }} />
        <input ref={fileRef} type="file" accept=".csv,text/csv" onChange={onFile} style={{ display: "none" }} />
        <button onClick={() => fileRef.current?.click()}>Upload Workiz CSV</button>
        {stored && <button onClick={clearJobs}>Clear</button>}
      </div>

      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        Spam is approximated by call length — the transcript classifier has analysed almost
        none of these calls, so a duration floor is the only reliable filter. Job and revenue
        data stays in this browser; nothing is written to the database.
      </p>

      {stored ? (
        <p className="muted" style={{ fontSize: 12 }}>
          {stored.jobs.length} jobs from <strong>{stored.fileName}</strong>, uploaded{" "}
          {new Date(stored.uploadedAt).toLocaleString()}.
        </p>
      ) : (
        <div className="card" style={{ marginBottom: 16 }}>
          <strong>No job data yet.</strong>
          <p className="muted" style={{ marginBottom: 0 }}>
            Export your jobs from Workiz as CSV and upload it above. Until then only the call
            side of the picture is available.
          </p>
        </div>
      )}

      {parseErrors.length > 0 && (
        <div className="card" style={{ marginBottom: 16, borderColor: "#ffb020" }}>
          {parseErrors.map((e) => (
            <div key={e} style={{ fontSize: 12 }}>⚠ {e}</div>
          ))}
        </div>
      )}

      {!range ? (
        <p className="muted">Pick a start and end date.</p>
      ) : calls.isLoading ? (
        <div>Loading…</div>
      ) : calls.error ? (
        <div className="card">Could not load call data: {String((calls.error as any).message)}</div>
      ) : (
        <Body
          rows={rows}
          jobs={attributed}
          callData={calls.data}
          matching={matches.isLoading}
          matched={matches.data?.matched ?? 0}
          phoneCount={phones.length}
          hasJobs={!!stored}
        />
      )}
    </div>
  );
}

function Body({
  rows, jobs, callData, matching, matched, phoneCount, hasJobs,
}: {
  rows: CampaignRow[]; jobs: AttributedJob[]; callData: any;
  matching: boolean; matched: number; phoneCount: number; hasJobs: boolean;
}) {
  const revenue = jobs.filter((j) => j.outcome === "won").reduce((s, j) => s + j.total, 0);
  const won = jobs.filter((j) => j.outcome === "won").length;
  const lost = jobs.filter((j) => j.outcome === "lost").length;
  const verified = jobs.filter((j) => j.basis === "call").length;
  // Deduplicated across campaigns: the same client must not be counted twice in the header.
  const convertedTotal = new Set(
    jobs.filter((j) => j.basis === "call" && j.phone).map((j) => j.phone),
  ).size;
  const convertingTotal = rows.reduce((s, r) => s + r.convertingCalls, 0);
  const conflicts = jobs.filter((j) => j.conflictsWith).length;
  const filtered = callData.total_calls_in_range - callData.total_qualified_calls;

  // Best-value campaign by revenue per qualified call — the headline "put money here" answer.
  // Requires real call volume, otherwise one lucky job off two calls would win it.
  const best = rows
    .filter((r) => r.campaign !== UNATTRIBUTED && r.qualifiedCalls >= 10 && r.revenue > 0)
    .sort((a, b) => (b.revenuePerCall ?? 0) - (a.revenuePerCall ?? 0))[0];

  // Recharts wants one row per month with a column per campaign.
  const monthly = useMemo(() => {
    const named = rows.slice(0, 5).map((r) => r.campaign);
    const byMonth = new Map<string, any>();
    for (const p of callData.monthly ?? []) {
      if (!named.includes(p.campaign)) continue;
      if (!byMonth.has(p.month)) byMonth.set(p.month, { month: p.month });
      byMonth.get(p.month)[p.campaign] = p.qualified_calls;
    }
    return Array.from(byMonth.values()).sort((a, b) => a.month.localeCompare(b.month));
  }, [callData.monthly, rows]);

  const series = rows.slice(0, 5).map((r) => r.campaign);

  return (
    <>
      <div className="row" style={{ marginBottom: 16 }}>
        <Stat n={callData.total_qualified_calls} l="Qualified calls"
              hint={`${filtered.toLocaleString()} shorter calls filtered out`} />
        <Stat
          n={hasJobs ? convertedTotal : "—"}
          l="Callers who became clients"
          hint={hasJobs ? `${convertingTotal} of ${callData.total_qualified_calls} calls` : undefined}
        />
        <Stat n={hasJobs ? jobs.length : "—"} l="Jobs in range" />
        <Stat n={hasJobs ? won : "—"} l="Jobs won" hint={lost ? `${lost} lost` : undefined} />
        <Stat n={hasJobs ? money0(revenue) : "—"} l="Revenue (won)" />
        <Stat
          n={hasJobs && callData.total_qualified_calls ? money0(revenue / callData.total_qualified_calls) : "—"}
          l="Revenue per qualified call"
        />
      </div>

      {best && (
        <div className="card" style={{ marginBottom: 16, borderColor: "#37d67a" }}>
          <strong>{best.campaign}</strong> is returning the most per call —{" "}
          {money0(best.revenuePerCall ?? 0)} of revenue for every qualified call, from{" "}
          {best.qualifiedCalls} calls and {best.won} closed {best.won === 1 ? "job" : "jobs"}{" "}
          ({money0(best.revenue)}).
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            Revenue per call is the fair comparison here — it rewards a channel for sending
            fewer, better leads. Add ad spend later to turn this into true ROAS.
          </div>
        </div>
      )}

      <div className="card" style={{ marginBottom: 16, overflowX: "auto" }}>
        <div className="l" style={{ marginBottom: 8 }}>Campaign scoreboard</div>
        <table>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>Campaign</th>
              <th style={{ textAlign: "right" }}>Qualified calls</th>
              <th style={{ textAlign: "right" }}>Unique callers</th>
              <th style={{ textAlign: "right" }}>Became clients</th>
              <th style={{ textAlign: "right" }}>Converting calls</th>
              <th style={{ textAlign: "right" }}>Caller→client</th>
              <th style={{ textAlign: "right" }}>Jobs</th>
              <th style={{ textAlign: "right" }}>Won</th>
              <th style={{ textAlign: "right" }}>Close rate</th>
              <th style={{ textAlign: "right" }}>Revenue</th>
              <th style={{ textAlign: "right" }}>Avg job</th>
              <th style={{ textAlign: "right" }}>Rev / call</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.campaign}>
                <td>
                  {r.campaign}
                  {r.jobs > 0 && r.callVerified < r.jobs && (
                    <span className="muted" style={{ fontSize: 11 }}>
                      {" "}({r.callVerified}/{r.jobs} call-verified)
                    </span>
                  )}
                </td>
                <td style={{ textAlign: "right" }}>{r.qualifiedCalls || "—"}</td>
                <td style={{ textAlign: "right" }}>{r.uniqueCallers || "—"}</td>
                <td style={{ textAlign: "right", fontWeight: 600 }}>{r.convertedCallers || "—"}</td>
                <td style={{ textAlign: "right" }}>
                  {r.convertingCalls ? `${r.convertingCalls} of ${r.qualifiedCalls}` : "—"}
                </td>
                <td style={{ textAlign: "right" }}>{pct(r.callerConversion)}</td>
                <td style={{ textAlign: "right" }}>{r.jobs || "—"}</td>
                <td style={{ textAlign: "right" }}>{r.won || "—"}</td>
                <td style={{ textAlign: "right" }}>{pct(r.closeRate)}</td>
                <td style={{ textAlign: "right" }}>{r.revenue ? money0(r.revenue) : "—"}</td>
                <td style={{ textAlign: "right" }}>{r.avgJobValue ? money0(r.avgJobValue) : "—"}</td>
                <td style={{ textAlign: "right", fontWeight: 600 }}>
                  {r.revenuePerCall ? money0(r.revenuePerCall) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          <strong>Became clients</strong> counts the distinct people who called this campaign
          and turned into a Workiz job — per caller, so a repeat client counts once.{" "}
          <strong>Converting calls</strong> is how many of the campaign's qualified calls came
          from those people. Both count only call-verified jobs: a job attributed from Workiz's
          Source field alone was never tied to a real call, so it cannot prove a call converted.
          Close rate is won ÷ (won + lost) — undecided jobs are excluded. {UNATTRIBUTED} holds
          calls whose tracking number has no campaign and jobs whose phone matched no call.
        </div>
      </div>

      <div className="row">
        <div className="card chartcard" style={{ flex: 1, minWidth: 360, height: 280 }}>
          <div className="l">Revenue per qualified call</div>
          <ResponsiveContainer width="100%" height="90%">
            <BarChart data={rows.filter((r) => r.revenuePerCall)}>
              <CartesianGrid stroke="#2a2f3a" />
              <XAxis dataKey="campaign" {...AXIS} />
              <YAxis {...AXIS} />
              <Tooltip contentStyle={TIP} formatter={(v: any) => money0(Number(v))} />
              <Bar dataKey="revenuePerCall" fill="#37d67a" />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card chartcard" style={{ flex: 1, minWidth: 360, height: 280 }}>
          <div className="l">Qualified calls vs jobs won</div>
          <ResponsiveContainer width="100%" height="90%">
            <BarChart data={rows}>
              <CartesianGrid stroke="#2a2f3a" />
              <XAxis dataKey="campaign" {...AXIS} />
              <YAxis {...AXIS} allowDecimals={false} />
              <Tooltip contentStyle={TIP} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="qualifiedCalls" name="Qualified calls" fill="#4f8cff" />
              <Bar dataKey="won" name="Jobs won" fill="#ffb020" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <div className="card chartcard" style={{ flex: 1, minWidth: 360, height: 280 }}>
          <div className="l">Qualified calls per month (Miami time)</div>
          <ResponsiveContainer width="100%" height="90%">
            <LineChart data={monthly}>
              <CartesianGrid stroke="#2a2f3a" />
              <XAxis dataKey="month" {...AXIS} />
              <YAxis {...AXIS} allowDecimals={false} />
              <Tooltip contentStyle={TIP} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {series.map((c, i) => (
                <Line key={c} type="monotone" dataKey={c} stroke={COLORS[i % COLORS.length]}
                      strokeWidth={2} dot={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {hasJobs && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="l" style={{ marginBottom: 8 }}>How trustworthy is this?</div>
          <ul style={{ fontSize: 13, margin: 0, paddingLeft: 18 }}>
            <li>
              {matching ? "Matching…" : `${matched} of ${phoneCount} job phone numbers`} matched a
              qualified call. {verified} of {jobs.length} jobs are attributed from a real call;
              the rest fall back to Workiz's own Source field or stay unattributed.
            </li>
            <li>
              {conflicts === 0
                ? "No job's Workiz Source contradicts the tracking number the caller actually dialled."
                : `${conflicts} job(s) have a Workiz Source that contradicts the tracking number dialled — the dialled number wins.`}
            </li>
            <li>
              {filtered.toLocaleString()} calls in this range were shorter than the floor and
              excluded. Raise or lower the threshold above to see how sensitive the picture is.
            </li>
          </ul>
        </div>
      )}

      {hasJobs && (
        <div className="card" style={{ marginTop: 16, overflowX: "auto" }}>
          <div className="l" style={{ marginBottom: 8 }}>Jobs ({jobs.length})</div>
          <table>
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>Job #</th>
                <th style={{ textAlign: "left" }}>Client</th>
                <th style={{ textAlign: "left" }}>Type</th>
                <th style={{ textAlign: "left" }}>Campaign</th>
                <th style={{ textAlign: "left" }}>Workiz source</th>
                <th style={{ textAlign: "left" }}>Status</th>
                <th style={{ textAlign: "right" }}>Total</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.jobNumber}>
                  <td>{j.jobNumber}</td>
                  <td>{j.client || "—"}</td>
                  <td>{j.type || "—"}</td>
                  <td>
                    {j.campaign ?? <span className="muted">unattributed</span>}{" "}
                    {j.basis === "call" && <span className="badge new">call</span>}
                    {j.basis === "workiz" && <span className="badge prov">workiz</span>}
                    {j.conflictsWith && (
                      <span className="badge spam" title={`Workiz says ${j.conflictsWith}`}>conflict</span>
                    )}
                  </td>
                  <td>{j.source || "—"}</td>
                  <td>
                    {j.status}
                    {j.outcome === "won" && <span className="badge new"> won</span>}
                    {j.outcome === "lost" && <span className="badge spam"> lost</span>}
                  </td>
                  <td style={{ textAlign: "right" }}>{j.total ? money0(j.total) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
