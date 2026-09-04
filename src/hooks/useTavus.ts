import { useCallback, useRef, useState } from "react";
import type {
  DailyCall,
  DailyEventObjectAppMessage,
  DailyEventObjectTrack,
} from "@daily-co/daily-js";
import { api } from "@/lib/api";

// Drives an optional Tavus video-avatar layer. It stays completely dormant
// until `activate()` is called, and even then does nothing unless the backend
// reports Tavus is configured (enabled:false by default) — so with no key set,
// the Daily SDK is never even imported and the app behaves exactly as before.
//
// The avatar runs in ECHO mode: Crucible's agents stay in control and we send
// the interviewer's lines to the replica to speak. Tavus/Daily's live video
// path can only be verified with a real key; the disabled fallback is the
// default and is fully covered.

export type TavusStatus = "idle" | "connecting" | "ready" | "error" | "disabled";

export function useTavus() {
  const [status, setStatus] = useState<TavusStatus>("idle");
  const [speaking, setSpeaking] = useState(false);
  const [videoTrack, setVideoTrack] = useState<MediaStreamTrack | null>(null);

  const callRef = useRef<DailyCall | null>(null);
  const conversationIdRef = useRef<string | null>(null);
  const audioElRef = useRef<HTMLAudioElement | null>(null);

  const activate = useCallback(async () => {
    if (status !== "idle") return;
    setStatus("connecting");
    try {
      const session = await api.avatarSession();
      if (!session.enabled || !session.conversationUrl) {
        setStatus("disabled");
        return;
      }
      conversationIdRef.current = session.conversationId;

      // Import the heavy WebRTC SDK only once we know Tavus is actually on.
      const { default: Daily } = await import("@daily-co/daily-js");
      const call = Daily.createCallObject({ subscribeToTracksAutomatically: true });
      callRef.current = call;

      // With createCallObject() nothing plays automatically — we render the
      // replica's video ourselves AND must play its audio ourselves, or it's
      // silent. Grab both tracks; audio goes straight to an <audio> sink.
      const onTrack = (ev: DailyEventObjectTrack) => {
        if (ev.participant?.local) return;
        if (ev?.track?.kind === "video") {
          setVideoTrack(ev.track as MediaStreamTrack);
        } else if (ev?.track?.kind === "audio") {
          const el = (audioElRef.current ??= new Audio());
          el.srcObject = new MediaStream([ev.track]);
          el.autoplay = true;
          void el.play().catch(() => {/* gesture already granted via VOICE */});
        }
      };
      // Mirror the avatar's speaking state onto the UI for lip-sync cues.
      const onAppMessage = (ev: DailyEventObjectAppMessage) => {
        const t = ev?.data?.event_type;
        if (t === "conversation.replica.started_speaking") setSpeaking(true);
        if (t === "conversation.replica.stopped_speaking") setSpeaking(false);
      };
      call.on("track-started", onTrack);
      call.on("app-message", onAppMessage);

      // We only RECEIVE from Tavus (echo mode). Don't publish our cam/mic into
      // the room — the candidate's answers reach the agents via our own STT.
      await call.join({ url: session.conversationUrl, startVideoOff: true, startAudioOff: true });
      setStatus("ready");
    } catch (err) {
      console.error("[useTavus] activation failed:", err);
      setStatus("error");
    }
  }, [status]);

  // Echo mode: tell the replica exactly what to say (agents stay in control).
  const speak = useCallback((text: string) => {
    const call = callRef.current;
    const conversationId = conversationIdRef.current;
    if (!call || !conversationId || !text.trim()) return;
    call.sendAppMessage(
      {
        message_type: "conversation",
        event_type: "conversation.echo",
        conversation_id: conversationId,
        properties: { text },
      },
      "*"
    );
  }, []);

  const deactivate = useCallback(async () => {
    // End the billed Tavus conversation first so we don't leak minutes.
    if (conversationIdRef.current) {
      void api.endAvatarSession(conversationIdRef.current);
    }
    const call = callRef.current;
    if (call) {
      try {
        await call.leave();
        call.destroy();
      } catch {
        /* already torn down */
      }
    }
    if (audioElRef.current) {
      audioElRef.current.pause();
      audioElRef.current.srcObject = null;
      audioElRef.current = null;
    }
    callRef.current = null;
    conversationIdRef.current = null;
    setVideoTrack(null);
    setSpeaking(false);
    setStatus("idle");
  }, []);

  const ready = status === "ready";
  return { activate, deactivate, speak, status, ready, speaking, videoTrack };
}
