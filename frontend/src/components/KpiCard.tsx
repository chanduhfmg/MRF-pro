import type { ReactNode } from "react";
import GlassCard from "./GlassCard";
import { useCountUp } from "../hooks/useCountUp";
import { Skeleton } from "./Skeleton";

interface Props {
  label: string;
  value: number;
  format?: (n: number) => string;
  sub?: ReactNode;
  icon?: ReactNode;
  loading?: boolean;
  delay?: number;
  accent?: boolean;
}

export default function KpiCard({
  label,
  value,
  format = (n) => Math.round(n).toLocaleString("en-US"),
  sub,
  icon,
  loading = false,
  delay = 0,
  accent = false,
}: Props) {
  const animated = useCountUp(loading ? 0 : value);

  return (
    <GlassCard hover delay={delay} className="p-5">
      <div className="flex items-start justify-between">
        <span className="text-[13px] font-medium text-dim">{label}</span>
        {icon && (
          <span className={`grid h-8 w-8 place-items-center rounded-lg ${accent ? "bg-accent-600/20 text-accent-400" : "bg-white/[0.05] text-faint"}`}>
            {icon}
          </span>
        )}
      </div>
      {loading ? (
        <Skeleton className="mt-3 h-9 w-28" />
      ) : (
        <div className="mt-2 text-[30px] font-semibold leading-none tracking-tighter2 tnum">
          {format(animated)}
        </div>
      )}
      {sub && <div className="mt-2 text-xs text-faint">{sub}</div>}
    </GlassCard>
  );
}
