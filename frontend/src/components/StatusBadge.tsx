import type { JobStatus } from "../lib/types";

const MAP: Record<
  JobStatus,
  { label: string; dot: string; text: string; ring: string; pulse?: boolean }
> = {
  queued: {
    label: "Queued",
    dot: "bg-zinc-400",
    text: "text-zinc-300",
    ring: "ring-zinc-500/20 bg-zinc-500/10",
  },
  processing: {
    label: "Processing",
    dot: "bg-amber-400",
    text: "text-amber-200",
    ring: "ring-amber-500/25 bg-amber-500/10",
    pulse: true,
  },
  completed: {
    label: "Completed",
    dot: "bg-accent-400",
    text: "text-accent-400",
    ring: "ring-emerald-500/25 bg-emerald-500/10",
  },
  failed: {
    label: "Failed",
    dot: "bg-red-400",
    text: "text-red-300",
    ring: "ring-red-500/25 bg-red-500/10",
  },
};

export default function StatusBadge({
  status,
  size = "sm",
}: {
  status: JobStatus;
  size?: "sm" | "xs";
}) {
  const s = MAP[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full ring-1 ${s.ring} ${s.text} ${
        size === "xs" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs"
      } font-medium`}
    >
      <span className={`relative flex h-1.5 w-1.5`}>
        {s.pulse && (
          <span className={`absolute inline-flex h-full w-full rounded-full ${s.dot} opacity-75 animate-ping`} />
        )}
        <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${s.dot}`} />
      </span>
      {s.label}
    </span>
  );
}
