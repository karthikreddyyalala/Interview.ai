import { useCallback, useEffect, useRef, useState } from "react";
import type { KokoroTTS } from "kokoro-js";

export type KokoroStatus = "idle" | "loading" | "ready" | "error";

// Lazily loads the Kokoro-82M ONNX model (~82MB quantized, cached by the
// browser after first download). The public interface matches useSpeechSynthesis
// so Interview.tsx can swap them without touching every callsite.
export function useKokoroTTS() {
  const [status, setStatus] = useState<KokoroStatus>("idle");
  const [speaking, setSpeaking] = useState(false);

  const ttsRef = useRef<KokoroTTS | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);

  const cancel = useCallback(() => {
    try {
      sourceRef.current?.stop();
    } catch {
      // already stopped — safe to ignore
    }
    sourceRef.current = null;
    setSpeaking(false);
  }, []);

  const load = useCallback(async () => {
    if (ttsRef.current || status !== "idle") return;
    setStatus("loading");
    try {
      const { KokoroTTS } = await import("kokoro-js");
      ttsRef.current = await KokoroTTS.from_pretrained(
        "onnx-community/Kokoro-82M-v1.0",
        { dtype: "q8" }
      );
      setStatus("ready");
    } catch (err) {
      console.error("[KokoroTTS] model load failed:", err);
      setStatus("error");
    }
  }, [status]);

  const speak = useCallback(
    async (text: string) => {
      if (!ttsRef.current || !text.trim()) return;
      cancel();
      setSpeaking(true);
      try {
        const result = await ttsRef.current.generate(text, { voice: "af_heart" });
        const ctx = (audioCtxRef.current ??= new AudioContext());
        const buf = ctx.createBuffer(1, result.audio.length, result.sampling_rate);
        buf.copyToChannel(new Float32Array(result.audio), 0);
        const src = ctx.createBufferSource();
        sourceRef.current = src;
        src.buffer = buf;
        src.connect(ctx.destination);
        src.onended = () => setSpeaking(false);
        src.start();
      } catch {
        setSpeaking(false);
      }
    },
    [cancel]
  );

  useEffect(() => cancel, [cancel]);

  return { load, status, speak, cancel, speaking };
}
