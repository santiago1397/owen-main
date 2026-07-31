/** Workiz job-export parsing + campaign attribution — ALL of it in the browser.
 *
 *  The attribution module is read-only against OWEN's database: job and revenue data is
 *  never sent to the server or stored in Postgres. The operator uploads a Workiz CSV, it is
 *  parsed here, and it lives in localStorage until the next upload. The only thing that ever
 *  goes to the backend is the list of phone numbers, so calls can be resolved to campaigns.
 */

export type JobOutcome = "won" | "lost" | "open";

export type WorkizJob = {
  jobNumber: string;
  client: string;
  type: string;
  status: string;
  outcome: JobOutcome;
  source: string;       // Workiz's own, human-entered acquisition claim
  isAhs: boolean;
  phoneRaw: string;
  phone: string | null; // E.164, null when the row has no usable number
  total: number;
  createdAt: Date | null;
  scheduledAt: Date | null;
  city: string;
  tech: string;
};

/** Workiz status -> commercial outcome. Revenue is only ever counted for `won`.
 *  Anything unmapped falls to `open` so a new Workiz status can never be silently
 *  banked as revenue.
 *
 *  ONLY `Done` is won (spec W4, aligned here 2026-07-28). This module previously also
 *  banked `done pending approval`, `Submitted` and `Pending (Collect Balance)` as won,
 *  which made this page report higher revenue than the same jobs show in GHL — the two
 *  were computing "won" by different rules and could never reconcile. `done pending
 *  approval` in particular is revenue AT RISK: AHS has not approved it, and treating it
 *  as earned overstates every campaign that carries AHS work.
 *
 *  STATUS_MAP in backend/app/scripts/import_workiz.py is the same table — keep them in step. */
const OUTCOME: Record<string, JobOutcome> = {
  "Done": "won",
  "Canceled": "lost",
  "done pending approval": "open",
  "Submitted": "open",
  "Pending (Collect Balance)": "open",
  "Pending (Estimate Follow Up)": "open",
  "Pending (New Roof Estimate)": "open",
  "In Progress (Inspections)": "open",
  "In Progress (Repair Schedule)": "open",
  "In Progress (Callback)": "open",
  "In Progress (Request Approval)": "open",
};

/** Workiz writes inconsistent internal whitespace — the 2026-07-28 export contains
 *  "In Progress (Request Approval  )" with two spaces before the paren. Looking that up
 *  verbatim misses, and the job silently falls through to `open` with a spurious "unknown
 *  status" warning. Mirrors norm_status() in app/scripts/import_workiz.py. */
export function normStatus(s: string): string {
  return (s || "").trim().replace(/\s+/g, " ").replace(/ \)/g, ")").replace(/\( /g, "(");
}

/** Workiz `Source` values that are a genuine acquisition-channel claim, mapped to the OWEN
 *  campaign they correspond to. Everything else (AHS, "Existig Customer", AI) is not a
 *  channel claim at all, so it can never be used as an attribution fallback. Mirrors
 *  CHANNEL_SRC in app/scripts/import_workiz.py — keep the two in step. */
export const SOURCE_TO_CAMPAIGN: Record<string, string> = {
  "Google": "GBP",
  "CL- ADS": "Craiglist",
  "FB": "Facebook",
};

/** Obvious placeholder numbers in the export (e.g. +11111111111) match nothing and would
 *  otherwise look like a real unmatched lead. */
const JUNK_PHONES = new Set(["+11111111111", "+10000000000", "+11234567890"]);

export function toE164(raw: string): string | null {
  const d = (raw || "").replace(/\D/g, "");
  const e = d.length === 10 ? "+1" + d : d.length === 11 && d.startsWith("1") ? "+" + d : null;
  return e && !JUNK_PHONES.has(e) ? e : null;
}

/** RFC-4180 parser: the export quotes fields containing commas ("Estimate, Followup") and
 *  escapes a literal quote by doubling it, so splitting on commas corrupts real rows. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  // Strip the UTF-8 BOM: the file is utf-8-sig, and a leading BOM would corrupt the first
  // header name so every lookup of "Job #" would miss.
  const s = text.replace(/^﻿/, "");

  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quoted) {
      if (c === '"') {
        if (s[i + 1] === '"') { field += '"'; i++; } else quoted = false;
      } else field += c;
      continue;
    }
    if (c === '"') quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (c !== "\r") field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((f) => f.trim() !== ""));
}

/** Workiz writes "Thu Jul 23, 2026 09:34 am". Date.parse chokes on the leading weekday and
 *  the lowercase meridiem, so pull the parts out explicitly. Returns null rather than an
 *  Invalid Date so callers can treat "no date" as a first-class case. */
const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

export function parseWorkizDate(raw: string): Date | null {
  const m = (raw || "").match(/([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{4})(?:\s+(\d{1,2}):(\d{2})\s*([ap]m))?/i);
  if (!m) return null;
  const month = MONTHS.indexOf(m[1].toLowerCase());
  if (month < 0) return null;
  let hour = m[4] ? parseInt(m[4], 10) % 12 : 0;
  if (m[6] && m[6].toLowerCase() === "pm") hour += 12;
  const d = new Date(parseInt(m[3], 10), month, parseInt(m[2], 10), hour, m[5] ? parseInt(m[5], 10) : 0);
  return isNaN(d.getTime()) ? null : d;
}

function money(raw: string): number {
  const n = parseFloat((raw || "").replace(/[$,]/g, ""));
  return isNaN(n) ? 0 : n;
}

export function parseWorkizCsv(text: string): { jobs: WorkizJob[]; errors: string[] } {
  const rows = parseCsv(text);
  const errors: string[] = [];
  if (!rows.length) return { jobs: [], errors: ["The file is empty."] };

  const header = rows[0].map((h) => h.trim());
  const idx = (name: string) => header.findIndex((h) => h.toLowerCase() === name.toLowerCase());
  const cols = {
    jobNumber: idx("Job #"), client: idx("Client"), type: idx("Type"), status: idx("Status"),
    source: idx("Source"), phone: idx("Phone"), total: idx("Total"),
    created: idx("Job Created"), scheduled: idx("Scheduled"), city: idx("City"), tech: idx("Tech"),
  };
  if (cols.jobNumber < 0 || cols.status < 0 || cols.total < 0) {
    return {
      jobs: [],
      errors: ["This does not look like a Workiz job export — expected 'Job #', 'Status' and 'Total' columns."],
    };
  }

  const at = (r: string[], i: number) => (i >= 0 ? (r[i] ?? "").trim() : "");
  const jobs: WorkizJob[] = [];

  for (const r of rows.slice(1)) {
    const jobNumber = at(r, cols.jobNumber);
    if (!jobNumber) continue;
    const status = normStatus(at(r, cols.status));
    const source = at(r, cols.source);
    if (!OUTCOME[status]) errors.push(`Unknown Workiz status "${status}" on job ${jobNumber} — counted as open.`);
    const phoneRaw = at(r, cols.phone);
    jobs.push({
      jobNumber,
      client: at(r, cols.client),
      type: at(r, cols.type),
      status,
      outcome: OUTCOME[status] ?? "open",
      source,
      isAhs: source.toUpperCase() === "AHS",
      phoneRaw,
      phone: toE164(phoneRaw),
      total: money(at(r, cols.total)),
      createdAt: parseWorkizDate(at(r, cols.created)),
      scheduledAt: parseWorkizDate(at(r, cols.scheduled)),
      city: at(r, cols.city),
      tech: at(r, cols.tech),
    });
  }
  // One line per bad status would flood the banner; collapse to the distinct set.
  return { jobs, errors: Array.from(new Set(errors)) };
}

// --- attribution ------------------------------------------------------------------------

export type PhoneMatch = {
  phone: string;
  qualified_calls: number;
  first_campaign: string | null;
  first_call_at: string | null;
  last_campaign: string | null;
  last_call_at: string | null;
  total_talk_seconds: number;
};

/** How a job got its campaign — surfaced in the UI so the operator can see how much of the
 *  picture is call-verified versus taken on trust from Workiz's own Source field. */
export type Basis = "call" | "workiz" | "none";

export type AttributedJob = WorkizJob & {
  campaign: string | null;
  basis: Basis;
  /** Set when a call-verified campaign contradicts Workiz's Source claim. */
  conflictsWith: string | null;
  match: PhoneMatch | null;
};

export const UNATTRIBUTED = "(unattributed)";

/** First-touch attribution: the campaign behind the EARLIEST qualified call from that phone
 *  — the channel that discovered the lead, not the one it happened to ring back on. Falls
 *  back to Workiz's Source only when it is a real channel claim and no call matched. */
export function attribute(jobs: WorkizJob[], matches: PhoneMatch[]): AttributedJob[] {
  const byPhone = new Map(matches.map((m) => [m.phone, m]));

  return jobs.map((j) => {
    const match = j.phone ? byPhone.get(j.phone) ?? null : null;
    const called = match?.first_campaign ?? null;
    const claimed = SOURCE_TO_CAMPAIGN[j.source] ?? null;

    if (called) {
      return {
        ...j, match, campaign: called, basis: "call" as Basis,
        conflictsWith: claimed && claimed !== called ? claimed : null,
      };
    }
    if (claimed) return { ...j, match, campaign: claimed, basis: "workiz" as Basis, conflictsWith: null };
    return { ...j, match, campaign: null, basis: "none" as Basis, conflictsWith: null };
  });
}

export type CampaignRow = {
  campaign: string;
  qualifiedCalls: number;
  uniqueCallers: number;
  jobs: number;
  won: number;
  lost: number;
  open: number;
  revenue: number;
  avgJobValue: number;
  /** won / (won + lost) — open jobs are excluded, they have not been decided yet. */
  closeRate: number | null;
  /** jobs / qualifiedCalls — how often a qualified call turns into any job at all. */
  jobRate: number | null;
  /** The money question: revenue earned per qualified call this campaign delivered. */
  revenuePerCall: number | null;
  callVerified: number;

  // --- "how many of these calls actually became a client?" ---------------------------
  /** Distinct people (phone numbers) who called this campaign AND turned into a Workiz job.
   *  Counted per caller, not per job, so a repeat client is one converted caller. Only
   *  call-verified jobs count — a job attributed from Workiz's Source field alone was never
   *  tied to a real call, so it cannot prove a call converted. */
  convertedCallers: number;
  /** Of this campaign's qualified calls, how many came from someone who became a client.
   *  This is the literal "how many of those calls translated into jobs" figure. */
  convertingCalls: number;
  /** convertedCallers / uniqueCallers — the campaign's true lead-to-client rate. */
  callerConversion: number | null;
};

export function rollUp(
  jobs: AttributedJob[],
  callStats: { campaign: string; qualified_calls: number; unique_callers: number }[],
): CampaignRow[] {
  const rows = new Map<string, CampaignRow>();
  const blank = (campaign: string): CampaignRow => ({
    campaign, qualifiedCalls: 0, uniqueCallers: 0, jobs: 0, won: 0, lost: 0, open: 0,
    revenue: 0, avgJobValue: 0, closeRate: null, jobRate: null, revenuePerCall: null,
    callVerified: 0, convertedCallers: 0, convertingCalls: 0, callerConversion: null,
  });
  // Per campaign, the distinct phones that converted. A client with two jobs must not be
  // counted twice, and their calls must not be added twice either.
  const converted = new Map<string, Map<string, number>>();
  const get = (c: string) => {
    if (!rows.has(c)) rows.set(c, blank(c));
    return rows.get(c)!;
  };

  for (const s of callStats) {
    const r = get(s.campaign);
    r.qualifiedCalls = s.qualified_calls;
    r.uniqueCallers = s.unique_callers;
  }
  for (const j of jobs) {
    const key = j.campaign ?? UNATTRIBUTED;
    const r = get(key);
    r.jobs++;
    if (j.basis === "call") r.callVerified++;
    r[j.outcome]++;
    if (j.outcome === "won") r.revenue += j.total;

    // Only a call-verified job proves a call turned into a client.
    if (j.basis === "call" && j.phone && j.match) {
      if (!converted.has(key)) converted.set(key, new Map());
      converted.get(key)!.set(j.phone, j.match.qualified_calls);
    }
  }

  for (const [campaign, phones] of converted) {
    const r = get(campaign);
    r.convertedCallers = phones.size;
    r.convertingCalls = Array.from(phones.values()).reduce((s, n) => s + n, 0);
  }

  for (const r of rows.values()) {
    const decided = r.won + r.lost;
    r.avgJobValue = r.won ? r.revenue / r.won : 0;
    r.closeRate = decided ? r.won / decided : null;
    r.jobRate = r.qualifiedCalls ? r.jobs / r.qualifiedCalls : null;
    r.revenuePerCall = r.qualifiedCalls ? r.revenue / r.qualifiedCalls : null;
    r.callerConversion = r.uniqueCallers ? r.convertedCallers / r.uniqueCallers : null;
  }

  // Best earner first; campaigns that produced calls but no revenue sink to the bottom
  // rather than disappearing — "this channel rang and never paid" is a finding too.
  return Array.from(rows.values()).sort(
    (a, b) => b.revenue - a.revenue || b.qualifiedCalls - a.qualifiedCalls,
  );
}

export const money0 = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
export const pct = (n: number | null) => (n === null ? "—" : `${(n * 100).toFixed(0)}%`);
