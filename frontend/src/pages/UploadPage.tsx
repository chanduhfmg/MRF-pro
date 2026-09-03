import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import Dropzone from "../components/Dropzone";
import ProgressBar from "../components/ProgressBar";
import { api } from "../lib/api";
import { formatBytes } from "../lib/format";
import type { Meta } from "../lib/types";

const FALLBACK_COMPANIES = [
  { key: "uhc", label: "UnitedHealthcare (UHC)" },
  { key: "aetna", label: "Aetna" },
  { key: "mvp", label: "MVP Health Care" },
  { key: "anthem", label: "Anthem" },
];

type Phase = "idle" | "uploading" | "finalizing" | "error";

export default function UploadPage({ meta }: { meta: Meta | null }) {
  const navigate = useNavigate();
  const companies = meta?.companies?.length ? meta.companies : FALLBACK_COMPANIES;

  const [company, setCompany] = useState(companies[0]?.key ?? "uhc");
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [sentBytes, setSentBytes] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const uploadIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!companies.find((c) => c.key === company)) setCompany(companies[0]?.key ?? "uhc");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (uploadIdRef.current) api.abortUpload(uploadIdRef.current).catch(() => {});
    },
    [],
  );

  const busy = phase === "uploading" || phase === "finalizing";
  const pct = file && file.size > 0 ? (sentBytes / file.size) * 100 : 0;

  async function start() {
    if (!file) return;
    setError(null);
    setPhase("uploading");
    setSentBytes(0);
    const ac = new AbortController();
    abortRef.current = ac;

    try {
      const { upload_id, chunk_size } = await api.initUpload({
        insurance_company: company,
        filename: file.name,
        file_size: file.size,
      });
      uploadIdRef.current = upload_id;

      const total = Math.max(1, Math.ceil(file.size / chunk_size));
      for (let i = 0; i < total; i++) {
        if (ac.signal.aborted) throw new DOMException("aborted", "AbortError");
        const startB = i * chunk_size;
        const blob = file.slice(startB, Math.min(file.size, startB + chunk_size));
        await api.uploadChunk(upload_id, i, blob, ac.signal);
        setSentBytes(Math.min(file.size, startB + blob.size));
      }

      setPhase("finalizing");
      const job = await api.completeUpload(upload_id, total);
      uploadIdRef.current = null;
      navigate(`/jobs/${job.id}`);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        setPhase("idle");
        return;
      }
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  function cancel() {
    abortRef.current?.abort();
    if (uploadIdRef.current) {
      api.abortUpload(uploadIdRef.current).catch(() => {});
      uploadIdRef.current = null;
    }
    setPhase("idle");
    setSentBytes(0);
  }

  return (
    <PageTransition>
      <PageHeader
        title="Upload MRF"
        subtitle="Hand off a Transparency-in-Coverage in-network rates file for extraction & packaging."
      />

      <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
        <GlassCard className="p-6">
          <label className="mb-2 block text-[13px] font-medium text-dim">Insurance company</label>
          <div className="mb-6 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {companies.map((c) => (
              <button
                key={c.key}
                type="button"
                disabled={busy}
                onClick={() => setCompany(c.key)}
                className={`rounded-xl border px-3 py-2.5 text-left text-xs transition ${
                  company === c.key
                    ? "border-accent-400/50 bg-accent-600/15 text-white"
                    : "border-white/10 bg-white/[0.02] text-dim hover:border-white/20 hover:text-white"
                }`}
              >
                <span className="block text-[11px] uppercase tracking-wide text-faint">{c.key}</span>
                <span className="font-medium">{c.label.replace(/\s*\(.*\)/, "")}</span>
              </button>
            ))}
          </div>

          <label className="mb-2 block text-[13px] font-medium text-dim">MRF file</label>
          <Dropzone file={file} onFile={setFile} disabled={busy} />

          {(busy || phase === "error") && file && (
            <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="mt-5">
              <ProgressBar
                percent={phase === "finalizing" ? 100 : pct}
                stage={
                  phase === "finalizing"
                    ? "Assembling & queueing…"
                    : phase === "error"
                      ? "Upload failed"
                      : `Uploading — ${formatBytes(sentBytes)} / ${formatBytes(file.size)}`
                }
                active={phase === "uploading"}
                failed={phase === "error"}
              />
              {error && <p className="mt-2 text-xs text-red-300">{error}</p>}
            </motion.div>
          )}

          <div className="mt-6 flex items-center gap-3">
            {!busy ? (
              <button className="btn btn-primary" disabled={!file} onClick={start}>
                {phase === "error" ? "Retry upload" : "Start extraction"}
              </button>
            ) : (
              <button className="btn btn-ghost" onClick={cancel}>
                Cancel
              </button>
            )}
            <span className="text-xs text-faint">
              Chunked upload &mdash; multi-GB files stream in {formatBytes(meta?.upload_chunk_size ?? 8388608)} parts.
            </span>
          </div>
        </GlassCard>

        <GlassCard className="h-fit p-5" delay={0.08}>
          <h3 className="text-sm font-semibold tracking-tightish">What happens next</h3>
          <ol className="mt-3 space-y-3 text-xs text-dim">
            {[
              ["Queued", "A job record is created and picked up by the background worker."],
              ["Building provider lookup", "Streams provider_references to map group → TIN + NPIs."],
              ["Extracting rates", "ijson pass over in_network for the target CPT/HCPCS codes."],
              ["Enriching NPIs", "Threaded NPPES lookups add provider names & addresses."],
              ["Packaging", "Rows split into CSVs and zipped for download."],
            ].map(([k, v], i) => (
              <li key={k} className="flex gap-3">
                <span className="mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full bg-white/[0.06] text-[10px] text-faint tnum">
                  {i + 1}
                </span>
                <span>
                  <span className="font-medium text-[var(--text)]">{k}.</span> {v}
                </span>
              </li>
            ))}
          </ol>
          {meta?.extraction_mode === "stub" && (
            <p className="mt-4 rounded-lg bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 ring-1 ring-amber-500/20">
              Engine is in <b>stub</b> mode — progress & output are simulated.
            </p>
          )}
        </GlassCard>
      </div>
    </PageTransition>
  );
}
