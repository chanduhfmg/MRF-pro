import { useEffect, useRef, useState } from "react";

/**
 * Turns a server ETA that arrives every few seconds into a value that ticks
 * down every second in between, like a countdown.
 *
 * Each new server value re-anchors the clock - unless it agrees with where
 * the countdown already is (within a few seconds), in which case the anchor
 * is kept so the display never jumps *up* just because the poll returned the
 * same rounded number again.
 */
export function useCountdown(
  seconds: number | null | undefined,
  active = true,
): number | null {
  const anchor = useRef<{ value: number; at: number } | null>(null);
  const [display, setDisplay] = useState<number | null>(seconds ?? null);

  useEffect(() => {
    if (seconds == null) {
      anchor.current = null;
      setDisplay(null);
      return;
    }
    const a = anchor.current;
    const projected = a ? a.value - (Date.now() - a.at) / 1000 : null;
    if (projected == null || Math.abs(seconds - projected) > 4) {
      anchor.current = { value: seconds, at: Date.now() };
      setDisplay(seconds);
    }
  }, [seconds]);

  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => {
      const a = anchor.current;
      if (!a) return;
      setDisplay(Math.max(0, a.value - (Date.now() - a.at) / 1000));
    }, 1000);
    return () => window.clearInterval(t);
  }, [active]);

  return active ? display : (seconds ?? null);
}
