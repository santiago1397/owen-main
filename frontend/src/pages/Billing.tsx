import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { AlertTriangle, PhoneIncoming, PhoneOutgoing } from "lucide-react";
import { api } from "../api";
import DateRangeBar from "../components/DateRangeBar";
import type { Range } from "../lib/dates";

// Usage figures here are BULKVS'S OWN rated amounts (their GET /voice feed), not an
// estimate — each row is what they actually charged. The recurring block IS derived locally
// from the numbers inventory, since per-DID monthly/E911/setup have no call record behind
// them. Two things drive the page's shape:
//   1. Charges are per LEG, because that is what BulkVS meters. A call the flow forwards back
//      out over the same trunk bills TWICE (inbound + outbound), seconds apart, so a
//      per-call view would understate a forwarded call by roughly half.
//   2. At this account's volume the RECURRING per-DID fees dwarf usage, so they lead the page.

// Money here is genuinely tiny (a 6-second increment of the cheapest tier is $0.00003), so
// rounding to cents would render almost every real charge as "$0.00".
function money(n: number | null | undefined, dp = 4): string {
  return `$${(n ?? 0).toFixed(dp)}`;
}
function fmtPhone(p: string | null | undefined): string {
  if (!p) return "";
  const d = p.replace(/\D/g, "");
  if (d.length === 11 && d.startsWith("1")) return `(${d.slice(1, 4)}) ${d.slice(4, 7)}-${d.slice(7)}`;
  if (d.length === 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  return p;
}
function fmtSecs(s: number | null | undefined): string {
  const v = s ?? 0;
  if (v < 60) return `${v}s`;
  return `${Math.floor(v / 60)}m ${v % 60}s`;
}
function fmtWhen(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
  });
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

export default function Billing() {
  const [range, setRange] = useState<Range | null>(null);
  const [dirFilter, setDirFilter] = useState<string>("");
  const params = range
    ? { date_from: range.from.toISOString(), date_to: range.to.toISOString() }
    : null;

  const summary = useQuery({
    queryKey: ["billing-summary", params],
    queryFn: () => api.billingSummary(params!),
    enabled: !!params,
  });
  const legs = useQuery({
    queryKey: ["billing-legs", params, dirFilter],
    queryFn: () => api.billingLegs({ ...params!, direction: dirFilter || undefined, limit: 300 }),
    enabled: !!params,
  });

  const s = summary.data;
  const rec = s?.recurring;

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0 }}>Billing</h2>
        <DateRangeBar defaultPreset="30d" onChange={setRange} />
      </div>

      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        Call costs are <strong>BulkVS's own rated amounts</strong>, pulled from their billing
        records — not an estimate. Charges are per <strong>leg</strong>, which is what they
        meter: a call your flow forwards back out over the trunk bills twice, once inbound and
        once outbound. Billed in {s?.increment_seconds ?? 6}-second increments. CNAM lookups
        ($0.002 per inbound call) and the monthly per-number fees are calculated from the
        published price sheet — BulkVS deducts those from the balance without itemising them.
      </p>

      {!range && <p className="muted">Pick a date range.</p>}
      {summary.isError && (
        <div className="card">Could not load billing: {String((summary.error as any)?.message)}</div>
      )}

      {s && (
        <>
          {/* Recurring first: at low call volume this IS the bill. */}
          <div className="card" style={{ marginBottom: 16, overflowX: "auto" }}>
            <div className="l" style={{ marginBottom: 8 }}>Monthly recurring</div>
            <table className="table">
              <thead>
                <tr>
                  <th>Number</th><th>Name</th><th>Tier</th>
                  <th style={{ textAlign: "right" }}>Monthly</th>
                  <th style={{ textAlign: "right" }}>E911</th>
                  <th style={{ textAlign: "right" }}>Setup (this period)</th>
                </tr>
              </thead>
              <tbody>
                {(rec?.numbers ?? []).map((n: any) => (
                  <tr key={n.number_id}>
                    <td>{fmtPhone(n.phone_number)}</td>
                    <td className="muted">{n.friendly_name || "—"}</td>
                    <td>
                      {n.tier ?? (
                        <span style={{ color: "#ffb020" }} title="No tier synced — cannot price">
                          unknown
                        </span>
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>{money(n.monthly, 2)}</td>
                    <td style={{ textAlign: "right" }}>{n.e911 ? money(n.e911, 2) : "—"}</td>
                    <td style={{ textAlign: "right" }}>
                      {n.setup_this_period ? money(n.setup_this_period, 2) : "—"}
                    </td>
                  </tr>
                ))}
                {!(rec?.numbers ?? []).length && (
                  <tr><td colSpan={6} className="muted">No active BulkVS numbers.</td></tr>
                )}
              </tbody>
            </table>
            <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
              Whole months, not prorated. BulkVS proration behaviour is unconfirmed and the
              error is a fraction of a cent at these rates.
            </div>
          </div>

          <div className="row" style={{ marginBottom: 16 }}>
            <Stat n={money(s.grand_total, 2)} l="Total"
                  hint="recurring + usage + adjustments" />
            <Stat n={money(rec?.monthly_total, 2)} l="Recurring / month" />
            <Stat n={money(s.usage_total)} l="Usage this period" />
            <Stat
              n={s.unrated_legs}
              l="Unrated legs"
              hint={s.unrated_legs ? "not priced — excluded from the total" : "all legs priced"}
            />
          </div>

          {/* Anything we could not price is stated outright rather than counted as $0. */}
          {!!s.unrated?.length && (
            <div className="card" style={{ marginBottom: 16, borderColor: "#ffb020" }}>
              <div className="l" style={{ marginBottom: 6 }}>
                <AlertTriangle size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
                Legs with no rate
              </div>
              {s.unrated.map((u: any, i: number) => (
                <div key={i} className="muted" style={{ fontSize: 12 }}>
                  {u.legs} leg{u.legs === 1 ? "" : "s"} ({fmtSecs(u.raw_billsec)}) — {u.reason}
                </div>
              ))}
              <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
                These are excluded from the total. They are shown rather than priced at $0 so
                the estimate never quietly under-reports.
              </div>
            </div>
          )}

          <div className="card" style={{ marginBottom: 16, overflowX: "auto" }}>
            <div className="l" style={{ marginBottom: 8 }}>Usage breakdown</div>
            <table className="table">
              <thead>
                <tr>
                  <th>Charge</th><th>Direction</th>
                  <th style={{ textAlign: "right" }}>Legs</th>
                  <th style={{ textAlign: "right" }}>Billed</th>
                  <th style={{ textAlign: "right" }}>Rate</th>
                  <th style={{ textAlign: "right" }}>Cost</th>
                </tr>
              </thead>
              <tbody>
                {(s.usage_lines ?? []).map((u: any, i: number) => (
                  <tr key={i}>
                    <td>{u.rate_label}</td>
                    <td>
                      {u.direction === "inbound" ? (
                        <><PhoneIncoming size={12} style={{ verticalAlign: -1 }} /> in</>
                      ) : (
                        <><PhoneOutgoing size={12} style={{ verticalAlign: -1 }} /> out</>
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>{u.legs}</td>
                    <td style={{ textAlign: "right" }}>
                      {u.kind === "cnam" ? "—" : fmtSecs(u.billed_seconds)}
                    </td>
                    <td style={{ textAlign: "right" }} className="muted">
                      {money(u.rate_amount, 4)}{u.kind === "cnam" ? " ea" : "/min"}
                    </td>
                    <td style={{ textAlign: "right" }}>{money(u.amount)}</td>
                  </tr>
                ))}
                {!(s.usage_lines ?? []).length && (
                  <tr><td colSpan={6} className="muted">No priced usage in this period.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {!!(s.per_day ?? []).length && (
            <div className="card" style={{ marginBottom: 16, overflowX: "auto" }}>
              <div className="l" style={{ marginBottom: 8 }}>Per day</div>
              <table className="table">
                <thead>
                  <tr>
                    <th>Day</th>
                    <th style={{ textAlign: "right" }}>Legs</th>
                    <th style={{ textAlign: "right" }}>Billed</th>
                    <th style={{ textAlign: "right" }}>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {s.per_day.map((d: any) => (
                    <tr key={d.day}>
                      <td>{d.day}</td>
                      <td style={{ textAlign: "right" }}>{d.legs}</td>
                      <td style={{ textAlign: "right" }}>{fmtSecs(d.billed_seconds)}</td>
                      <td style={{ textAlign: "right" }}>{money(d.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <div className="card" style={{ overflowX: "auto" }}>
        <div className="toolbar" style={{ marginBottom: 8, gap: 8 }}>
          <div className="l">Billable legs</div>
          <select value={dirFilter} onChange={(e) => setDirFilter(e.target.value)}>
            <option value="">All directions</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </select>
          {legs.data && (
            <span className="muted" style={{ fontSize: 12 }}>
              {legs.data.total} leg{legs.data.total === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>When</th><th>Dir</th><th>Our number</th><th>Other party</th>
              <th style={{ textAlign: "right" }}>Answered</th>
              <th style={{ textAlign: "right" }}>Billed</th>
              <th style={{ textAlign: "right" }}>Rate</th>
              <th style={{ textAlign: "right" }}>Cost</th>
            </tr>
          </thead>
          <tbody>
            {(legs.data?.items ?? []).map((l: any) => (
              <tr key={l.id} style={l.unrated ? { color: "#ffb020" } : undefined}>
                <td>{fmtWhen(l.at)}</td>
                <td>{l.direction === "inbound" ? "in" : "out"}</td>
                <td>{fmtPhone(l.our_number) || <span className="muted">—</span>}</td>
                <td>
                  {l.kind === "cnam" ? (
                    <span className="muted" title="Caller-name lookup, billed per inbound call">
                      CNAM lookup
                    </span>
                  ) : l.dest_unknown ? (
                    <span className="muted" title="Legacy row: destination was not recorded">
                      (not recorded)
                    </span>
                  ) : (
                    fmtPhone(l.other_party) || <span className="muted">—</span>
                  )}
                </td>
                <td style={{ textAlign: "right" }}>
                  {l.kind === "cnam" ? "—" : fmtSecs(l.raw_billsec)}
                </td>
                <td style={{ textAlign: "right" }}>
                  {l.kind === "cnam" ? "—" : fmtSecs(l.billed_seconds)}
                </td>
                <td style={{ textAlign: "right" }} className="muted">
                  {l.rate_amount != null ? money(l.rate_amount, 4) : "—"}
                </td>
                <td style={{ textAlign: "right" }} title={l.unrated_reason || undefined}>
                  {l.unrated ? "unrated" : money(l.amount)}
                </td>
              </tr>
            ))}
            {!(legs.data?.items ?? []).length && (
              <tr><td colSpan={8} className="muted">No billable legs in this period.</td></tr>
            )}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          Amounts are BulkVS's own rated charges, refreshed from their billing records every
          10 minutes.
        </div>
      </div>
    </div>
  );
}
