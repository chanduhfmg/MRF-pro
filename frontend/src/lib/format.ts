export function formatBytes(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  if (n === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / Math.pow(1024, i);
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

export function formatNumber(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US");
}

export function formatCompact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h < 24) return mm ? `${h}h ${mm}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const hh = h % 24;
  return hh ? `${d}d ${hh}h` : `${d}d`;
}

export function formatEta(seconds: number | null | undefined): string {
  if (seconds == null || seconds <= 0) return "—";
  return `~${formatDuration(seconds)} left`;
}

export function formatPercent(p: number | null | undefined, digits = 0): string {
  if (p == null || Number.isNaN(p)) return "—";
  return `${p.toFixed(digits)}%`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const diffSec = (d.getTime() - Date.now()) / 1000; // negative = past
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const divisions: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
    [4.34524, "week"],
    [12, "month"],
    [Number.POSITIVE_INFINITY, "year"],
  ];
  let value = diffSec;
  for (const [amount, unit] of divisions) {
    if (Math.abs(value) < amount) return rtf.format(Math.round(value), unit);
    value /= amount;
  }
  return d.toLocaleDateString();
}

/** Transfer rate, e.g. "12.4 MB/s". */
export function formatRate(bytesPerSecond: number | null | undefined): string {
  if (bytesPerSecond == null || Number.isNaN(bytesPerSecond) || bytesPerSecond <= 0)
    return "—";
  return `${formatBytes(bytesPerSecond)}/s`;
}

/**
 * ETA with the uncertainty made visible. A pipeline ETA is an extrapolation
 * from a rate that has not stopped changing, so showing "~4m left" is honest
 * while "4m 12s left" is not.
 */
export function formatEtaLong(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "estimating…";
  if (seconds === 0) return "finishing…";
  if (seconds < 10) return "a few seconds left";
  return `~${formatDuration(seconds)} left`;
}

/**
 * Countdown-style ETA: "1:04:32 left" / "4:32 left" / "0:07 left".
 * Whole seconds, always the same shape, so the eye can watch it tick.
 */
export function formatCountdown(seconds: number | null | undefined, suffix = " left"): string {
  if (seconds == null || Number.isNaN(seconds)) return "estimating…";
  const s = Math.max(0, Math.round(seconds));
  if (s === 0) return "finishing…";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  const ss = String(sec).padStart(2, "0");
  return `${h > 0 ? `${h}:` : ""}${mm}:${ss}${suffix}`;
}

/** "3 of 40" style progress counter. */
export function formatCount(done: number, total: number | null | undefined): string {
  if (!total) return formatNumber(done);
  return `${formatNumber(done)} of ${formatNumber(total)}`;
}
