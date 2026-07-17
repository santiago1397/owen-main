import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../api";
import DateRangeBar from "../components/DateRangeBar";
import { type Range, hourLabel } from "../lib/dates";

const COLORS = ["#4f8cff", "#37d67a", "#ffb020", "#ff5c6c", "#a78bfa", "#22d3ee"];

function Stat({ n, l }: { n: any; l: string }) {
  return (
    <div className="card stat">
      <div className="n">{n}</div>
      <div className="l">{l}</div>
    </div>
  );
}

export default function Dashboard() {
  const [range, setRange] = useState<Range | null>(null);
  const [hideJunk, setHideJunk] = useState(true);

  const { data, isLoading } = useQuery({
    queryKey: ["dashboard", range?.from.toISOString(), range?.to.toISOString(), hideJunk],
    queryFn: () =>
      api.dashboard({
        date_from: range!.from.toISOString(),
        date_to: range!.to.toISOString(),
        hide_junk: hideJunk,
      }),
    enabled: !!range,
  });

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0, flex: 1 }}>Dashboard</h2>
        <DateRangeBar defaultPreset="7d" onChange={setRange} />
      </div>

      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap", marginTop: 8 }}>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
          <input type="checkbox" checked={hideJunk} onChange={(e) => setHideJunk(e.target.checked)} />
          Hide likely-junk calls (≤3s or never connected)
        </label>
      </div>

      {!range ? (
        <p className="muted">Pick a start and end date.</p>
      ) : isLoading || !data ? (
        <div>Loading…</div>
      ) : (
        <DashboardBody data={data} hideJunk={hideJunk} />
      )}
    </div>
  );
}

function DashboardBody({ data, hideJunk }: { data: any; hideJunk: boolean }) {
  const donut = [
    { name: "New (campaign)", value: data.new_for_campaign },
    { name: "Returning", value: data.returning_for_campaign },
  ];
  const hourData = (data.by_hour ?? []).map((h: any) => ({ ...h, label: hourLabel(h.hour) }));

  return (
    <>
      <p className="muted" style={{ marginTop: 8, fontSize: 12 }}>
        {hideJunk ? "Likely-junk calls excluded from stats." : "Including likely-junk calls."}
      </p>

      <div className="row" style={{ marginBottom: 16 }}>
        <Stat n={data.total_calls} l="Total calls" />
        <Stat n={data.junk_calls} l="Likely junk" />
        <Stat n={data.spam_calls} l="Spam-flagged" />
        <Stat n={data.new_callers_global} l="New callers" />
        <Stat n={data.returning_callers_global} l="Returning callers" />
        <Stat n={data.avg_duration_seconds ? Math.round(data.avg_duration_seconds) + "s" : "—"} l="Avg duration" />
      </div>

      <div className="row">
        <div className="card" style={{ flex: 2, minWidth: 360, height: 260 }}>
          <div className="l">Calls per day (Miami time)</div>
          <ResponsiveContainer width="100%" height="90%">
            <LineChart data={data.daily}>
              <CartesianGrid stroke="#2a2f3a" />
              <XAxis dataKey="day" stroke="#9aa4b2" fontSize={11} />
              <YAxis stroke="#9aa4b2" fontSize={11} allowDecimals={false} />
              <Tooltip contentStyle={{ background: "#171a21", border: "1px solid #2a2f3a" }} />
              <Line type="monotone" dataKey="calls" stroke="#4f8cff" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>

        <div className="card" style={{ flex: 1, minWidth: 240, height: 260 }}>
          <div className="l">New vs returning (campaign)</div>
          <ResponsiveContainer width="100%" height="90%">
            <PieChart>
              <Pie data={donut} dataKey="value" nameKey="name" innerRadius={45} outerRadius={80}>
                {donut.map((_, i) => <Cell key={i} fill={COLORS[i]} />)}
              </Pie>
              <Tooltip contentStyle={{ background: "#171a21", border: "1px solid #2a2f3a" }} />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <div className="card" style={{ flex: 1, minWidth: 360, height: 260 }}>
          <div className="l">Call volume by hour (Miami time)</div>
          <ResponsiveContainer width="100%" height="90%">
            <BarChart data={hourData}>
              <CartesianGrid stroke="#2a2f3a" />
              <XAxis dataKey="label" stroke="#9aa4b2" fontSize={11} interval={2} />
              <YAxis stroke="#9aa4b2" fontSize={11} allowDecimals={false} />
              <Tooltip contentStyle={{ background: "#171a21", border: "1px solid #2a2f3a" }} />
              <Bar dataKey="calls" fill="#22d3ee" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <div className="card" style={{ flex: 1, minWidth: 360, height: 280 }}>
          <div className="l">Calls by campaign</div>
          <ResponsiveContainer width="100%" height="90%">
            <BarChart data={data.by_campaign}>
              <CartesianGrid stroke="#2a2f3a" />
              <XAxis dataKey="campaign" stroke="#9aa4b2" fontSize={11} />
              <YAxis stroke="#9aa4b2" fontSize={11} allowDecimals={false} />
              <Tooltip contentStyle={{ background: "#171a21", border: "1px solid #2a2f3a" }} />
              <Bar dataKey="calls" fill="#4f8cff" />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card" style={{ flex: 1, minWidth: 280 }}>
          <div className="l" style={{ marginBottom: 8 }}>Top callers</div>
          <table>
            <tbody>
              {data.top_callers.map((c: any) => (
                <tr key={c.phone}><td>{c.phone}</td><td style={{ textAlign: "right" }}>{c.calls}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
