import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

export type ExtractScope = "target" | "all";

/**
 * Asked every time an extraction is started: rows for just the provider that
 * was searched, or every provider group in the file that bills the chosen
 * codes. The difference is two to three orders of magnitude in output size
 * and enrichment time, so it is a decision, not a hidden default.
 */
export default function ExtractScopeDialog({
  open,
  fileCount,
  targetLabel,
  codesSummary,
  initialScope,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  fileCount: number;
  /** e.g. "TIN 473943680" */
  targetLabel: string;
  /** e.g. "default code set (78 codes)" or "4 custom codes" */
  codesSummary: string;
  initialScope: ExtractScope;
  busy?: boolean;
  onConfirm: (scope: ExtractScope) => void;
  onCancel: () => void;
}) {
  const [scope, setScope] = useState<ExtractScope>(initialScope);

  useEffect(() => {
    if (open) setScope(initialScope);
  }, [open, initialScope]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !busy) onCancel();
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="extract-scope-title"
        >
          <motion.div
            className="glass w-full max-w-[560px] p-6"
            initial={{ opacity: 0, y: 12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 320, damping: 28 }}
          >
            <h3 id="extract-scope-title" className="text-base font-semibold tracking-tightish">
              What should this extraction include?
            </h3>
            <p className="mt-1 text-xs text-dim">
              {fileCount === 1 ? "1 file" : `${fileCount} files`} · {codesSummary}. Every field the
              file carries is kept either way.
            </p>

            <div className="mt-5 space-y-3">
              <ScopeOption
                selected={scope === "target"}
                onSelect={() => setScope("target")}
                title={`Only ${targetLabel}`}
                badge="recommended"
                body="Rows for the provider you searched. Small output, enriches in minutes. This is what a rate lookup for one practice needs."
              />
              <ScopeOption
                selected={scope === "all"}
                onSelect={() => setScope("all")}
                title="Every provider that bills these codes"
                badge="large"
                badgeTone="amber"
                body="All provider groups in the file, for the chosen CPT/HCPCS codes — the whole network's rates. Hundreds of thousands of rows on a typical payer file, and NPPES enrichment runs for hours. Use this for a market-wide rate landscape, not one provider."
              />
            </div>

            <div className="mt-6 flex flex-wrap items-center justify-end gap-2">
              <button className="btn btn-ghost" disabled={busy} onClick={onCancel}>
                Cancel
              </button>
              <button className="btn btn-primary" disabled={busy} onClick={() => onConfirm(scope)}>
                {busy
                  ? "Queueing…"
                  : `Extract ${fileCount === 1 ? "this file" : `${fileCount} files`}`}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function ScopeOption({
  selected,
  onSelect,
  title,
  body,
  badge,
  badgeTone = "green",
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  body: string;
  badge?: string;
  badgeTone?: "green" | "amber";
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`flex w-full items-start gap-3 rounded-xl border p-4 text-left transition ${
        selected
          ? "border-accent-400/50 bg-accent-600/[0.12]"
          : "border-white/10 bg-white/[0.02] hover:border-white/20"
      }`}
      role="radio"
      aria-checked={selected}
    >
      <span
        className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full border ${
          selected ? "border-accent-400" : "border-white/30"
        }`}
      >
        {selected && <span className="h-2 w-2 rounded-full bg-accent-400" />}
      </span>
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-[var(--text)]">{title}</span>
          {badge && (
            <span
              className={`rounded-md px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                badgeTone === "amber"
                  ? "bg-amber-500/15 text-amber-300"
                  : "bg-accent-600/20 text-accent-400"
              }`}
            >
              {badge}
            </span>
          )}
        </span>
        <span className="mt-1 block text-xs leading-relaxed text-dim">{body}</span>
      </span>
    </button>
  );
}
