import { useCountdown } from "../hooks/useCountdown";
import { formatCountdown } from "../lib/format";

/**
 * A ticking ETA: "4:32 left" counting down every second, re-synced whenever
 * the server sends a fresh estimate. `active` false freezes it (finished or
 * queued jobs have nothing to count down).
 */
export default function Countdown({
  seconds,
  active = true,
  className = "",
  suffix = " left",
}: {
  seconds: number | null | undefined;
  active?: boolean;
  className?: string;
  suffix?: string;
}) {
  const s = useCountdown(seconds, active);
  return (
    <span className={`tnum ${className}`} title={s != null ? `${Math.round(s)} seconds` : undefined}>
      {formatCountdown(s, suffix)}
    </span>
  );
}
