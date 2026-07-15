import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../api";

const RANGES = ["today", "7d", "30d", "90d"];
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
  const [range, setRange] = useState("7d");
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard", range],
    queryFn: () => api.dashboard(range),
  });

  if (isLoading || !data) return <div>Loading…</div>;

  const donut = [
    { name: "New (campaign)", value: data.new_for_campaign },
    { name: "Returning", value: data.returning_for_campaign },
  ];

  return (
    <div>
      <div className="toolbar">
        <h2 style={{ margin: 0, flex: 1 }}>Dashboard</h2>
        <select value={range} onChange={(e) => setRange(e.target.value)}>
          {RANGES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>

      <div className="row" style={{ marginBottom: 16 }}>
        <Stat n={data.total_calls} l="Total calls" />
        <Stat n={data.spam_calls} l="Spam-flagged" />
        <Stat n={data.new_callers_global} l="New callers" />
        <Stat n={data.returning_callers_global} l="Returning callers" />
        <Stat n={data.avg_duration_seconds ? Math.round(data.avg_duration_seconds) + "s" : "—"} l="Avg duration" />
      </div>

      <div className="row">
        <div className="card" style={{ flex: 2, minWidth: 360, height: 260 }}>
          <div className="l">Calls per day (Eastern)</div>
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
    </div>
  );
}
