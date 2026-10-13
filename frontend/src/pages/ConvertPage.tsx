import { useRef, useState } from "react";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import Select from "../components/Select";
import { api, ApiError } from "../lib/api";
import { formatBytes } from "../lib/format";

type TargetFormat = "csv" | "xlsx" | "json";

const FORMATS: { value: TargetFormat; label: string }[] = [
  { value: "csv", label: "CSV" },
  { value: "xlsx", label: "Excel (.xlsx)" },
  { value: "json", label: "JSON" },
];

export default function ConvertPage() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [targetFormat, setTargetFormat] = useState<TargetFormat>("csv");
  const [converting, setConverting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ filename: string; size: number } | null>(null);

  function pickFile(f: File | null) {
    setFile(f);
    setError(null);
    setDone(null);
  }

  async function convert() {
    if (!file) return;
    setConverting(true);
    setError(null);
    setDone(null);
    try {
      const { blob, filename } = await api.convertFile(file, targetFormat);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setDone({ filename, size: blob.size });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
    } finally {
      setConverting(false);
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    const f = e.dataTransfer.files?.[0];
    if (f) pickFile(f);
  }

  return (
    <PageTransition>
      <PageHeader
        title="Convert File"
        subtitle="Upload a JSON, JSON.GZ, or CSV file and download it as something else."
      />

      <GlassCard className="p-6">
        <div
          onDragOver={(e) => e.preventDefault()}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
          className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-white/[0.12] bg-white/[0.02] px-6 py-10 text-center transition-colors hover:border-accent-500/40 hover:bg-white/[0.03]"
        >
          <UploadCloudIcon />
          {file ? (
            <div>
              <p className="text-sm font-medium">{file.name}</p>
              <p className="mt-0.5 text-[11px] text-faint">{formatBytes(file.size)}</p>
            </div>
          ) : (
            <div>
              <p className="text-sm font-medium">Drop a file here, or click to browse</p>
              <p className="mt-0.5 text-[11px] text-faint">.json, .json.gz, or .csv — up to 150 MB</p>
            </div>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,.gz,.csv,.txt"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
          />
        </div>

        <div className="mt-5 flex flex-wrap items-end gap-3">
          <div className="min-w-[200px]">
            <label className="mb-1.5 block text-xs font-medium text-dim">Convert to</label>
            <Select
              value={targetFormat}
              onChange={(v) => setTargetFormat(v as TargetFormat)}
              options={FORMATS}
            />
          </div>
          <button
            className="btn btn-primary"
            disabled={!file || converting}
            onClick={convert}
          >
            {converting ? "Converting…" : "Convert & download"}
          </button>
        </div>

        {error && (
          <p className="mt-4 text-sm text-red-300">{error}</p>
        )}
        {done && !error && (
          <p className="mt-4 text-sm text-emerald-300">
            Downloaded {done.filename} ({formatBytes(done.size)}).
          </p>
        )}

        <p className="mt-5 text-[11px] leading-relaxed text-faint">
          JSON input doesn&rsquo;t need to be any particular shape — a top-level
          array, or an object containing one, both work. Nested objects become
          dotted columns (e.g. <code>address.city</code>), lists of plain values
          join with &ldquo;;&rdquo;, and anything else nested is kept as a JSON string
          rather than dropped. This is a general-purpose tool for one-off files,
          not the multi-GB MRF pipeline — for payer extraction, use Search or
          Upload instead.
        </p>
      </GlassCard>
    </PageTransition>
  );
}

function UploadCloudIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-8 w-8 text-faint">
      <path
        d="M7 18a4 4 0 01-.6-7.96A5 5 0 0116.9 8.1 4.5 4.5 0 0117.5 17"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M12 12v7m0-7l-2.5 2.5M12 12l2.5 2.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
