// Audio device selection for the operator softphone (gear → Audio settings, Quo-style).
//
// Three per-browser preferences (localStorage), applied at three seams:
//   - microphone  -> getUserMedia deviceId constraint, read by softphone.ts at call time
//   - speaker     -> HTMLAudioElement.setSinkId() on the remote-call <audio> (live)
//   - ringtone    -> setSinkId() on a dedicated ring <audio> that loops while status==ringing
//
// Device ids are only stable/labelled once the user has granted mic permission, so the
// settings popover primes permission on open. Selecting "" (default) clears the pref and
// lets the OS default win. setSinkId is Chromium-only; on unsupported browsers speaker/
// ringtone routing is a no-op (selection still persists harmlessly).

import { useCallback, useEffect, useState } from "react";

const MIC_KEY = "owen_audio_mic";
const SPEAKER_KEY = "owen_audio_speaker";
const RINGTONE_KEY = "owen_audio_ringtone";

export type AudioKind = "mic" | "speaker" | "ringtone";

const KEY: Record<AudioKind, string> = {
  mic: MIC_KEY,
  speaker: SPEAKER_KEY,
  ringtone: RINGTONE_KEY,
};

export function getAudioPref(kind: AudioKind): string {
  return localStorage.getItem(KEY[kind]) || "";
}
export function setAudioPref(kind: AudioKind, deviceId: string) {
  if (deviceId) localStorage.setItem(KEY[kind], deviceId);
  else localStorage.removeItem(KEY[kind]);
}

// getUserMedia audio constraints honoring the saved mic. Read by softphone.ts at connect.
export function micConstraints(): MediaTrackConstraints | boolean {
  const id = getAudioPref("mic");
  return id ? { deviceId: { exact: id } } : true;
}

// Route an <audio> element to the saved output device. No-op if setSinkId is unsupported or
// the device vanished (falls back to default). `which` picks speaker vs ringtone pref.
export async function applySink(
  el: HTMLAudioElement | null,
  which: "speaker" | "ringtone",
): Promise<void> {
  if (!el) return;
  const sink = (el as unknown as { setSinkId?: (id: string) => Promise<void> }).setSinkId;
  if (typeof sink !== "function") return;
  const id = getAudioPref(which);
  try {
    await sink.call(el, id || "default");
  } catch {
    /* device gone / not permitted — leave on current sink */
  }
}

// --- Ringtone (synthesized, no bundled asset) --------------------------------------------
// A US ringback cadence (440+480 Hz, 2s on / 4s off) rendered once to a WAV data URI so the
// ring is self-contained. Cached across rings.
let _ringUri: string | null = null;
function ringbackWavUri(): string {
  if (_ringUri) return _ringUri;
  const rate = 8000;
  const onSec = 2;
  const totalSec = 6; // 2s tone + 4s silence, looped by the <audio>
  const total = rate * totalSec;
  const onSamples = rate * onSec;
  const bytesPerSample = 2;
  const buffer = new ArrayBuffer(44 + total * bytesPerSample);
  const view = new DataView(buffer);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  const dataLen = total * bytesPerSample;
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataLen, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * bytesPerSample, true); // byte rate
  view.setUint16(32, bytesPerSample, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, dataLen, true);
  for (let i = 0; i < total; i++) {
    let s = 0;
    if (i < onSamples) {
      const t = i / rate;
      s = (Math.sin(2 * Math.PI * 440 * t) + Math.sin(2 * Math.PI * 480 * t)) * 0.22;
    }
    const clamped = Math.max(-1, Math.min(1, s));
    view.setInt16(44 + i * bytesPerSample, clamped * 0x7fff, true);
  }
  // Base64-encode the buffer.
  let bin = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  _ringUri = `data:audio/wav;base64,${btoa(bin)}`;
  return _ringUri;
}

// The looping ring element (created once, lazily), routed to the saved ringtone device.
function ringEl(): HTMLAudioElement {
  let el = document.getElementById("softphone-ringtone") as HTMLAudioElement | null;
  if (!el) {
    el = document.createElement("audio");
    el.id = "softphone-ringtone";
    el.loop = true;
    el.src = ringbackWavUri();
    document.body.appendChild(el);
  }
  return el;
}

export async function startRingtone(): Promise<void> {
  const el = ringEl();
  await applySink(el, "ringtone");
  try {
    el.currentTime = 0;
    await el.play();
  } catch {
    /* autoplay blocked before any user gesture — the caller still sees the ringing pill */
  }
}
export function stopRingtone(): void {
  const el = document.getElementById("softphone-ringtone") as HTMLAudioElement | null;
  if (el) {
    el.pause();
    el.currentTime = 0;
  }
}

// --- Device enumeration hook (for the settings popover) ----------------------------------
export type MediaDeviceOption = { deviceId: string; label: string };
export type AudioDevices = { inputs: MediaDeviceOption[]; outputs: MediaDeviceOption[] };

export function useAudioDevices() {
  const [devices, setDevices] = useState<AudioDevices>({ inputs: [], outputs: [] });
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      // Prime permission so labels populate (no-op if already granted).
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());
    } catch {
      setError("Microphone access is blocked — allow it to pick devices.");
    }
    try {
      const list = await navigator.mediaDevices.enumerateDevices();
      const inputs = list
        .filter((d) => d.kind === "audioinput")
        .map((d, i) => ({ deviceId: d.deviceId, label: d.label || `Microphone ${i + 1}` }));
      const outputs = list
        .filter((d) => d.kind === "audiooutput")
        .map((d, i) => ({ deviceId: d.deviceId, label: d.label || `Speaker ${i + 1}` }));
      setDevices({ inputs, outputs });
      setReady(true);
    } catch (e: any) {
      setError(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onChange = () => void refresh();
    navigator.mediaDevices?.addEventListener?.("devicechange", onChange);
    return () => navigator.mediaDevices?.removeEventListener?.("devicechange", onChange);
  }, [refresh]);

  return { devices, ready, error, refresh };
}
