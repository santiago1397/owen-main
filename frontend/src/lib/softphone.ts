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

export type SoftphoneState = {
  status: SoftphoneStatus;
  available: boolean;
  error: string | null;
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
  });
  const userRef = useRef<Web.SimpleUser | null>(null);

  const patch = (p: Partial<SoftphoneState>) => setState((s) => ({ ...s, ...p }));

  const connect = useCallback(async () => {
    if (userRef.current) return;
    patch({ status: "registering", error: null });
    try {
      const creds: Credentials = await api.webrtcCredentials();
      const aor = `sip:${creds.sip.username}@${creds.sip.domain}`;
      const options: Web.SimpleUserOptions = {
        aor,
        media: { remote: { audio: remoteAudioEl() } },
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
            patch({ status: "ringing" });
          },
          onCallHangup: () => {
            patch({ status: state.available ? "available" : "offline" });
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
    patch({ status: "in-call" });
  }, []);

  const hangup = useCallback(async () => {
    const user = userRef.current;
    if (!user) return;
    await user.hangup();
    patch({ status: state.available ? "available" : "offline" });
  }, [state.available]);

  // Tear down on unmount so we don't leak a registration.
  useEffect(() => {
    return () => {
      void disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { state, setAvailable, answer, hangup };
}
