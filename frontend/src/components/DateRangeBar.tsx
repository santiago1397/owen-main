import { useEffect, useMemo, useRef, useState } from "react";
import {
  PRESET_LABELS, PRESETS, type Preset, type Range,
  easternTodayInput, resolveRange,
} from "../lib/dates";

// Preset buttons (Today / Yesterday / Last 7 / Last 30 / This month / Custom) plus native
// date inputs for the custom range. Owns its own selection state and reports the resolved
// UTC [from, to) window (or null while a custom range is incomplete) via onChange.
export default function DateRangeBar({
  defaultPreset = "7d",
  onChange,
}: {
  defaultPreset?: Preset;
  onChange: (range: Range | null) => void;
}) {
  const [now] = useState(() => new Date());
  const [preset, setPreset] = useState<Preset>(defaultPreset);
  const [customFrom, setCustomFrom] = useState(() => easternTodayInput(now));
  const [customTo, setCustomTo] = useState(() => easternTodayInput(now));

  const range = useMemo(
    () => resolveRange(preset, now, customFrom, customTo),
    [preset, now, customFrom, customTo],
  );

  // Report changes without depending on onChange's identity (parents pass inline fns).
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  useEffect(() => { onChangeRef.current(range); }, [range]);

  return (
    <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
      {PRESETS.map((p) => (
        <button
          key={p}
          onClick={() => setPreset(p)}
          style={{
            padding: "4px 10px",
            background: preset === p ? "#4f8cff" : "#1b1f27",
            color: preset === p ? "#fff" : "#9aa4b2",
            border: "1px solid #2a2f3a",
            borderRadius: 6,
            cursor: "pointer",
          }}
        >
          {PRESET_LABELS[p]}
        </button>
      ))}
      {preset === "custom" && (
        <span style={{ display: "flex", gap: 6, alignItems: "center", marginLeft: 4 }}>
          <input type="date" value={customFrom} max={customTo}
            onChange={(e) => setCustomFrom(e.target.value)} />
          <span className="muted">to</span>
          <input type="date" value={customTo} min={customFrom}
            onChange={(e) => setCustomTo(e.target.value)} />
        </span>
      )}
    </div>
  );
}
