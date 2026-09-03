import type { ReactNode } from "react";

export default function EmptyState({
  title,
  hint,
  action,
  icon,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-2xl bg-white/[0.04] text-faint ring-1 ring-white/[0.06]">
        {icon ?? <DefaultIcon />}
      </div>
      <div>
        <p className="text-sm font-medium text-[var(--text)]">{title}</p>
        {hint && <p className="mt-1 text-xs text-faint">{hint}</p>}
      </div>
      {action}
    </div>
  );
}

function DefaultIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5">
      <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.5" />
      <path d="M9 12h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
