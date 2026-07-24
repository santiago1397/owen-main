// Operator WebRTC softphone (Ticket 13).
//
// A thin wrapper over SIP.js's SimpleUser facade. SIP.js drives ONLY this operator's own leg:
// connect + register (via backend-minted creds), receive the incoming INVITE Asterisk sends to
// the per-operator `operator-<slug>` endpoint, answer, and hang up (BYE). ALL bridge/hold/
// blind-transfer go through the BACKEND over ARI (see api.telephonyHold/Bridge/Transfer) —
// this client NEVER talks to ARI.
//
// Credentials (SIP + ephemeral TURN iceServers) come from POST /api/telephony/webrtc/credentials
// at app-login time; the real gate is app login.
import { useCallback, useEffect, useRef, useState } from "react";
import { Web } from "sip.js";
import { api } from "../api";
import { applySink, micConstraints, startRingtone, stopRingtone } from "./audioDevices";

export type SoftphoneStatus =
  | "offline" // not registered (availability toggle off, or connect failed)
  | "registering"
  | "available" // registered + toggled available, waiting for calls
  | "ringing" // incoming INVITE pending answer
  | "in-call";

export type Credentials = {
  sip: {
    endpoint: string;
    username: string;
    authorization_username: string;
    password: string;
    domain: string;
    wss_url: string;
    expires_at: number;
  };
  ice_servers: { urls: string[]; username?: string; credential?: string }[];
};

// Who's calling on a pending incoming INVITE (Ticket 18). `caller` is the calling party's
// number (SIP From user); `dialed` is the display name Asterisk stamps = the DID that was
// dialed (so the popup can show "to what number"). Both are enriched to names in the UI.
export type IncomingInfo = { caller: string | null; dialed: string | null };

export type SoftphoneState = {
  status: SoftphoneStatus;
  available: boolean;
  error: string | null;
  incoming: IncomingInfo | null;
};

// The <audio> element the remote (caller) audio is routed into. Created once, lazily.
function remoteAudioEl(): HTMLAudioElement {
  let el = document.getElementById("softphone-remote-audio") as HTMLAudioElement | null;
  if (!el) {
    el = document.createElement("audio");
    el.id = "softphone-remote-audio";
    el.autoplay = true;
    document.body.appendChild(el);
  }
  // Route call audio to the operator's chosen speaker (live; no-op if unsupported).
  void applySink(el, "speaker");
  return el;
}

/**
 * React hook managing the operator softphone lifecycle. Availability = the app toggle AND a
 * live registration; flipping `setAvailable(false)` unregisters so the interpreter's
 * operator-target dial finds the endpoint offline and falls through to default_fallback.
 */
export function useSoftphone() {
  const [state, setState] = useState<SoftphoneState>({
    status: "offline",
    available: false,
    error: null,
    incoming: null,
  });
  const userRef = useRef<Web.SimpleUser | null>(null);

  // Set the moment the operator asks the backend to place an outbound call, and cleared by the
  // first INVITE that arrives (or on expiry). Held in a ref, not state, because onCallReceived
  // is a SIP.js callback captured at connect() time and would otherwise read a stale value.
  const outboundIntentRef = useRef<number>(0);

  // How long after clicking "call" an arriving INVITE is treated as our own outbound leg.
  // Generous enough for trunk setup, short enough that a real inbound call minutes later is
  // never auto-answered.
  const OUTBOUND_INTENT_TTL_MS = 45_000;

  const expectOutbound = useCallback(() => {
    outboundIntentRef.current = Date.now();
  }, []);

  // Single-shot: reading it clears it, so exactly one INVITE can ever be claimed per click.
  const consumeOutboundIntent = (): boolean => {
    const at = outboundIntentRef.current;
    outboundIntentRef.current = 0;
    return at > 0 && Date.now() - at < OUTBOUND_INTENT_TTL_MS;
  };

  const patch = (p: Partial<SoftphoneState>) => setState((s) => ({ ...s, ...p }));

  const connect = useCallback(async () => {
    if (userRef.current) return;
    patch({ status: "registering", error: null });
    try {
      const creds: Credentials = await api.webrtcCredentials();
      const aor = `sip:${creds.sip.username}@${creds.sip.domain}`;
      const options: Web.SimpleUserOptions = {
        aor,
        media: {
          // Honor the operator's chosen microphone (read fresh at connect time). SIP.js types
          // `audio` as boolean but forwards it verbatim to getUserMedia, so a deviceId
          // MediaTrackConstraints object is valid at runtime — cast at this boundary.
          constraints: { audio: micConstraints() as unknown as boolean, video: false },
          remote: { audio: remoteAudioEl() },
        },
        userAgentOptions: {
          authorizationUsername: creds.sip.authorization_username,
          authorizationPassword: creds.sip.password,
          transportOptions: { server: creds.sip.wss_url },
          sessionDescriptionHandlerFactoryOptions: {
            iceServers: creds.ice_servers.map((s) => ({
              urls: s.urls,
              username: s.username,
              credential: s.credential,
            })),
          },
        },
        delegate: {
          onCallReceived: async () => {
            // Read the incoming INVITE's identity so the popup can show who's calling and to
            // which DID. SimpleUser hides the session type, so reach it loosely: remoteIdentity
            // is the From header — .uri.user = caller number, .displayName = the dialed DID
            // (Asterisk stamps it as the operator leg's caller-ID name; see runtime._handle_unassigned).
            const s: any = (userRef.current as any)?.session;
            const rid = s?.remoteIdentity;
            const caller = (rid?.uri?.user as string) || null;
            const dialed = (rid?.displayName as string) || null;

            // A manual OUTBOUND call (Ticket 14) reaches this operator as an ordinary INVITE
            // too — the backend originates our leg first, then bridges it to the callee. It is
            // NOT an incoming call: showing the "Incoming call" popup for a number the operator
            // just dialed is wrong (and the caller-ID on that leg is the CALLEE). So when we
            // just asked the backend to place a call, auto-answer and go straight to in-call.
            // The window is short and single-shot so a genuine inbound call can never be
            // swallowed by a stale flag.
            if (consumeOutboundIntent()) {
              try {
                await userRef.current?.answer();
                patch({ status: "in-call", incoming: null });
                return;
              } catch {
                /* fall through to the normal incoming handling */
              }
            }
            patch({ status: "ringing", incoming: { caller, dialed } });
          },
          onCallHangup: () => {
            patch({ status: state.available ? "available" : "offline", incoming: null });
          },
          onCallAnswered: () => {
            patch({ status: "in-call" });
          },
        },
      };
      const user = new Web.SimpleUser(creds.sip.wss_url, options);
      userRef.current = user;
      await user.connect();
      await user.register();
      patch({ status: "available", available: true });
    } catch (e: any) {
      userRef.current = null;
      patch({ status: "offline", available: false, error: String(e?.message || e) });
    }
  }, [state.available]);

  const disconnect = useCallback(async () => {
    const user = userRef.current;
    userRef.current = null;
    try {
      if (user) {
        await user.unregister();
        await user.disconnect();
      }
    } catch {
      /* best-effort teardown */
    }
    patch({ status: "offline", available: false });
  }, []);

  const setAvailable = useCallback(
    (next: boolean) => {
      if (next) void connect();
      else void disconnect();
    },
    [connect, disconnect],
  );

  const answer = useCallback(async () => {
    const user = userRef.current;
    if (!user) return;
    await user.answer();
    patch({ status: "in-call", incoming: null });
  }, []);

  const decline = useCallback(async () => {
    // Reject a pending incoming INVITE (the operator opts out; Asterisk keeps ringing the
    // other operators and rolls to voicemail on the ring timeout).
    const user = userRef.current;
    if (!user) return;
    try {
      await user.decline();
    } catch {
      /* no pending invite */
    }
    patch({ status: state.available ? "available" : "offline", incoming: null });
  }, [state.available]);

  const hangup = useCallback(async () => {
    const user = userRef.current;
    if (!user) return;
    await user.hangup();
    patch({ status: state.available ? "available" : "offline", incoming: null });
  }, [state.available]);

  // Ring the operator's chosen ringtone device while an incoming call is pending; stop the
  // moment it's answered, hung up, or the phone goes offline.
  useEffect(() => {
    if (state.status === "ringing") void startRingtone();
    else stopRingtone();
  }, [state.status]);

  // Tear down on unmount so we don't leak a registration.
  useEffect(() => {
    return () => {
      stopRingtone();
      void disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { state, setAvailable, answer, decline, hangup, expectOutbound };
}

export type SoftphoneApi = ReturnType<typeof useSoftphone>;
