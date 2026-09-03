import { motion } from "framer-motion";
import { formatEta, formatPercent } from "../lib/format";

interface Props {
  percent: number;
  stage?: string | null;
  etaSeconds?: number | null;
  active?: boolean; // shimmer while true
  failed?: boolean;
  className?: string;
}

/**
 * Gradient-fill progress bar. Width animates smoothly (spring) so periodic
 * poll updates never jump. Shimmer sweep while active. Inline stage +
 * percent + ETA row underneath.
 */
export default function ProgressBar({
  percent,
  stage,
  etaSeconds,
  active = false,
  failed = false,
  className = "",
}: Props) {
  const pct = Math.max(0, Math.min(100, percent));
  return (
    <div className={className}>
      <div className="relative h-2.5 w-full overflow-hidden rounded-full bg-white/[0.06] ring-1 ring-inset ring-white/5">
        <motion.div
          className={`relative h-full rounded-full ${
            failed
              ? "bg-gradient-to-r from-red-600 to-red-400"
              : "bg-gradient-to-r from-accent-600 via-accent-500 to-accent-400"
          }`}
          initial={false}
          animate={{ width: `${pct}%` }}
          transition={{ type: "spring", stiffness: 120, damping: 22 }}
        >
          {active && !failed && <div className="absolute inset-0 sweep rounded-full" />}
          <div className="absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-white/30 to-transparent" />
        </motion.div>
      </div>
      <div className="mt-2 flex items-center justify-between text-xs">
        <span className={`font-medium ${failed ? "text-red-300" : "text-dim"}`}>
          {stage || (failed ? "Failed" : "Working")}
        </span>
        <span className="tnum text-faint">
          <span className={failed ? "text-red-300" : "text-[var(--text)]"}>{formatPercent(pct)}</span>
          {active && etaSeconds != null && etaSeconds > 0 && (
            <span className="ml-2">· {formatEta(etaSeconds)}</span>
          )}
        </span>
      </div>
    </div>
  );
}
