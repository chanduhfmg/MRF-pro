import type { ReactNode } from "react";

export function ChartFrame({
  title,
  subtitle,
  children,
  right,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold tracking-tightish">{title}</h3>
          {subtitle && <p className="mt-0.5 text-xs text-faint">{subtitle}</p>}
        </div>
        {right}
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}

export const AXIS = {
  stroke: "rgba(255,255,255,0.25)",
  tick: { fill: "rgba(255,255,255,0.45)", fontSize: 11 },
  tickLine: false,
  axisLine: false as const,
};

export function ChartTooltip({
  active,
  payload,
  label,
  formatter,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; color?: string }>;
  label?: string | number;
  formatter?: (v: number) => string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-white/10 bg-ink-900/95 px-3 py-2 text-xs shadow-xl backdrop-blur">
      {label != null && <div className="mb-1 font-medium text-[var(--text)]">{label}</div>}
      {payload.map((p, i) => (
        <div key={i} className="flex items-center gap-2 text-faint">
          <span className="h-2 w-2 rounded-full" style={{ background: p.color ?? "#3ddc84" }} />
          <span className="capitalize">{p.name}</span>
          <span className="ml-auto tnum text-[var(--text)]">
            {formatter ? formatter(p.value) : p.value.toLocaleString("en-US")}
          </span>
        </div>
      ))}
    </div>
  );
}
