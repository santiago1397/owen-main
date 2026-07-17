// Date-range helpers for the dashboard. All presets are computed against Miami/Eastern
// wall-clock time, then converted to UTC instants for the API (which filters started_at
// with a half-open [from, to) window — `to` is the start of the next day so the last day
// is fully included). No date library: uses Intl to derive the zone offset.

export const BUSINESS_TZ = "America/New_York";

type YMD = { y: number; m: number; d: number };

// Offset (ms) to add to a UTC instant to get the given tz's wall-clock, i.e. tzWall - utc.
function tzOffsetMs(tz: string, utcMs: number): number {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const p: Record<string, number> = {};
  for (const part of dtf.formatToParts(new Date(utcMs))) {
    if (part.type !== "literal") p[part.type] = Number(part.value);
  }
  const hour = p.hour === 24 ? 0 : p.hour; // some engines emit "24" at midnight
  const asUTC = Date.UTC(p.year, p.month - 1, p.day, hour, p.minute, p.second);
  return asUTC - utcMs;
}

// The UTC instant corresponding to midnight (00:00) of the given Eastern calendar day.
function easternMidnightUTC({ y, m, d }: YMD): Date {
  const guess = Date.UTC(y, m - 1, d, 0, 0, 0);
  // One correction pass is enough away from the exact DST switch instant.
  const offset = tzOffsetMs(BUSINESS_TZ, guess);
  return new Date(guess - offset);
}

// The Eastern calendar day (y/m/d) that a UTC instant falls on.
function easternYMD(date: Date): YMD {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BUSINESS_TZ, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date);
  const p: Record<string, number> = {};
  for (const part of parts) if (part.type !== "literal") p[part.type] = Number(part.value);
  return { y: p.year, m: p.month, d: p.day };
}

// Pure calendar arithmetic (tz-independent) via UTC normalization.
function addDays(ymd: YMD, n: number): YMD {
  const t = new Date(Date.UTC(ymd.y, ymd.m - 1, ymd.d + n));
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

export type Range = { from: Date; to: Date };

export const PRESETS = ["today", "yesterday", "7d", "30d", "month", "custom"] as const;
export type Preset = (typeof PRESETS)[number];

export const PRESET_LABELS: Record<Preset, string> = {
  today: "Today",
  yesterday: "Yesterday",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  month: "This month",
  custom: "Custom",
};

// "YYYY-MM-DD" -> YMD (as typed by an <input type="date">, interpreted as an Eastern day).
function parseInputDate(s: string): YMD | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  return m ? { y: +m[1], m: +m[2], d: +m[3] } : null;
}

// Today's Eastern calendar day, as a "YYYY-MM-DD" string (default for the custom inputs).
export function easternTodayInput(now: Date): string {
  const { y, m, d } = easternYMD(now);
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

// Resolve a preset (or custom from/to inputs) to a concrete UTC [from, to) window.
// Returns null for custom when the inputs are incomplete/invalid.
export function resolveRange(
  preset: Preset,
  now: Date,
  customFrom?: string,
  customTo?: string,
): Range | null {
  const today = easternYMD(now);
  switch (preset) {
    case "today":
      return { from: easternMidnightUTC(today), to: now };
    case "yesterday":
      return { from: easternMidnightUTC(addDays(today, -1)), to: easternMidnightUTC(today) };
    case "7d":
      return { from: new Date(now.getTime() - 7 * 86400_000), to: now };
    case "30d":
      return { from: new Date(now.getTime() - 30 * 86400_000), to: now };
    case "month":
      return { from: easternMidnightUTC({ ...today, d: 1 }), to: now };
    case "custom": {
      const f = customFrom && parseInputDate(customFrom);
      const t = customTo && parseInputDate(customTo);
      if (!f || !t) return null;
      // `to` is start of the day AFTER the picked end date → end date fully included.
      return { from: easternMidnightUTC(f), to: easternMidnightUTC(addDays(t, 1)) };
    }
  }
}

// 12-hour label for an hour-of-day bucket (0–23): 0->"12a", 13->"1p", etc.
export function hourLabel(h: number): string {
  const period = h < 12 ? "a" : "p";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${period}`;
}
