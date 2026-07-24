// Shared contact avatar — a colour-hashed circle with initials. Extracted from Inbox so the
// in-call panel renders participants with the exact same look as the conversation list.
const AVATAR_COLORS = ["#e0559b", "#5b8def", "#2fbf71", "#f0a03c", "#9b6cf0", "#ef6461", "#3cc8c8"];

export function avatarColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

export function initials(name: string | null, number: string | null): string {
  if (name && name.trim()) {
    const parts = name.trim().split(/\s+/);
    return (parts[0][0] + (parts[1]?.[0] || "")).toUpperCase();
  }
  const d = (number || "").replace(/\D/g, "");
  return d ? d.slice(-2) : "#";
}

export default function Avatar({
  name,
  number,
  size = 36,
}: {
  name: string | null;
  number: string | null;
  size?: number;
}) {
  const key = number || name || "?";
  return (
    <div
      className="quo-avatar"
      style={{ width: size, height: size, minWidth: size, background: avatarColor(key), fontSize: size * 0.38 }}
    >
      {initials(name, number)}
    </div>
  );
}
