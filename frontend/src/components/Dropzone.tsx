import { useCallback, useRef, useState } from "react";
import { motion } from "framer-motion";
import { formatBytes } from "../lib/format";

const ACCEPT = [".json", ".json.gz", ".gz"];

function accepted(name: string): boolean {
  const low = name.toLowerCase();
  return low.endsWith(".json") || low.endsWith(".json.gz") || low.endsWith(".gz");
}

export default function Dropzone({
  file,
  onFile,
  disabled = false,
}: {
  file: File | null;
  onFile: (f: File | null) => void;
  disabled?: boolean;
}) {
  const [over, setOver] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const pick = useCallback(
    (f: File | undefined | null) => {
      setErr(null);
      if (!f) return;
      if (!accepted(f.name)) {
        setErr("Only .json or .json.gz MRF files are accepted.");
        return;
      }
      onFile(f);
    },
    [onFile],
  );

  return (
    <div>
      <motion.div
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (disabled) return;
          pick(e.dataTransfer.files?.[0]);
        }}
        onClick={() => !disabled && inputRef.current?.click()}
        animate={{ scale: over ? 1.01 : 1 }}
        className={`relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-2xl border border-dashed px-6 py-12 text-center transition-colors ${
          disabled ? "cursor-not-allowed opacity-60" : ""
        } ${
          over
            ? "border-accent-400/70 bg-accent-600/[0.08]"
            : "border-white/15 bg-white/[0.02] hover:border-white/25 hover:bg-white/[0.035]"
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT.join(",")}
          className="hidden"
          disabled={disabled}
          onChange={(e) => pick(e.target.files?.[0])}
        />
        <div
          className={`grid h-12 w-12 place-items-center rounded-xl ${
            over ? "bg-accent-600/25 text-accent-400" : "bg-white/[0.05] text-faint"
          }`}
        >
          <svg viewBox="0 0 24 24" fill="none" className="h-6 w-6">
            <path
              d="M12 15V4m0 0L8 8m4-4l4 4"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <path
              d="M4 15v3.5A1.5 1.5 0 005.5 20h13a1.5 1.5 0 001.5-1.5V15"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        </div>

        {file ? (
          <div className="max-w-full">
            <p className="truncate text-sm font-medium text-[var(--text)]">{file.name}</p>
            <p className="mt-0.5 text-xs text-faint tnum">{formatBytes(file.size)}</p>
            <button
              type="button"
              className="mt-2 text-[11px] text-dim underline decoration-dotted underline-offset-2 hover:text-white"
              onClick={(e) => {
                e.stopPropagation();
                onFile(null);
                setErr(null);
              }}
            >
              choose a different file
            </button>
          </div>
        ) : (
          <div>
            <p className="text-sm font-medium text-[var(--text)]">
              Drop an MRF file here, or <span className="text-accent-400">browse</span>
            </p>
            <p className="mt-1 text-xs text-faint">
              Transparency-in-Coverage in-network rates &mdash; .json or .json.gz
            </p>
          </div>
        )}
      </motion.div>
      {err && <p className="mt-2 text-xs text-red-300">{err}</p>}
    </div>
  );
}
