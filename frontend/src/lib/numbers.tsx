// Shared helpers for the platform Numbers hub (Ticket 06).
// The numbers API (Ticket 03) returns owner_provider / media_provider / lifecycle;
// legacy Twilio/SignalWire DIDs leave the platform fields NULL.

export type NumberRow = {
  id: string;
  phone_number: string;
  friendly_name?: string | null;
  provider?: string | null;
  campaign_name?: string | null;
  forwards_to?: string | null;
  active: boolean;
  total_calls: number;
  last_call_at?: string | null;
  owner_provider?: string | null;
  media_provider?: string | null;
  released_at?: string | null;
  lifecycle: string; // available | assigned | released
};

const PLATFORM_PROVIDERS = ["bulkvs", "asterisk"];

// Only BulkVS/Asterisk-owned numbers get management affordances; everything else
// (legacy Twilio/SignalWire) is read-only in the platform hub.
export function isPlatformManaged(n: NumberRow): boolean {
  const owner = (n.owner_provider || "").toLowerCase();
  const media = (n.media_provider || "").toLowerCase();
  return PLATFORM_PROVIDERS.includes(owner) || PLATFORM_PROVIDERS.includes(media);
}

// Who owns (bills/routes) this DID today: ported numbers report their platform
// owner_provider even though the legacy provider row still says twilio/signalwire.
export function effectiveOwner(n: NumberRow): string {
  return (n.owner_provider || n.provider || "unknown").toLowerCase();
}

// "owner → media" one-liner, falling back to the legacy provider name.
export function providerPath(n: NumberRow): string {
  if (isPlatformManaged(n)) {
    return `${n.owner_provider || "—"} → ${n.media_provider || "—"}`;
  }
  return n.provider || "—";
}

export function LifecycleBadge({ lifecycle }: { lifecycle: string }) {
  return <span className={`badge lc-${lifecycle}`}>{lifecycle}</span>;
}
