import { Link, useParams } from "react-router-dom";
import { motion } from "framer-motion";
import PageTransition from "../components/PageTransition";
import GlassCard from "../components/GlassCard";
import StatusBadge from "../components/StatusBadge";
import ProgressBar from "../components/ProgressBar";
import { SkeletonText, Skeleton } from "../components/Skeleton";
import { useAsync } from "../hooks/useAsync";
import { useJobStatusPolling } from "../hooks/useJobStatusPolling";
import { api } from "../lib/api";
import type { Job } from "../lib/types";
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatEta,
  formatNumber,
  formatPercent,
} from "../lib/format";

export default function JobDetailPage() {
  const { id = "" } = useParams();
  const jobQ = useAsync(() => api.getJob(id), [id]);
  const job = jobQ.data;

  const live = useJobStatusPolling(id, job?.status, () => jobQ.silentRefetch());

  // merge live poll snapshot over the last full fetch
  const merged: Job | null = job
    ? {
        ...job,
        status: live?.status ?? job.status,
        percent_complete: live?.percent ?? job.percent_complete,
        eta_seconds: live?.eta_seconds ?? job.eta_seconds,
        current_stage: live?.stage ?? job.current_stage,
        records_processed: live?.records_processed ?? job.records_processed,
        bytes_processed: live?.bytes_processed ?? job.bytes_processed,
        error_message: live?.error_message ?? job.error_message,
      }
    : null;

  const running = merged?.status === "queued" || merged?.status === "processing";

  return (
    <PageTransition>
      <div className="mb-6">
        <Link to="/jobs" className="inline-flex items-center gap-1.5 text-xs text-dim hover:text-white">
          <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
            <path d="M14 6l-6 6 6 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          All jobs
        </Link>
      </div>

      {jobQ.loading && (
        <GlassCard className="p-6">
          <Skeleton className="h-7 w-64" />
          <SkeletonText className="mt-5" lines={4} />
        </GlassCard>
      )}

      {jobQ.error && (
        <GlassCard className="p-8 text-center text-sm text-red-300">{jobQ.error}</GlassCard>
      )}

      {merged && (
        <div className="space-y-6">
          <GlassCard className="p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-3">
                  <h1 className="truncate text-xl font-semibold tracking-tightish">
                    {merged.source_filename}
                  </h1>
                  <StatusBadge status={merged.status} />
                </div>
                <p className="mt-1 text-sm text-dim">
                  {merged.insurance_company_label} · job{" "}
                  <span className="tnum text-faint">{merged.id.slice(0, 8)}</span>
                </p>
              </div>
              {merged.has_output && (
                <a href={api.downloadUrl(merged.id)} className="btn btn-primary">
                  <DownloadIcon /> Download zip
                  <span className="ml-1 text-xs opacity-80">({formatBytes(merged.output_size_bytes)})</span>
                </a>
              )}
            </div>

            {running && (
              <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="mt-6">
                <ProgressBar
                  percent={merged.percent_complete}
                  stage={merged.current_stage}
                  etaSeconds={merged.eta_seconds}
                  active={merged.status === "processing"}
                />
                <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-faint tnum">
                  <span>{formatNumber(merged.records_processed)} records so far</span>
                  <span>
                    {formatBytes(merged.bytes_processed)} / {formatBytes(merged.total_bytes)} read
                  </span>
                  {merged.eta_seconds != null && merged.eta_seconds > 0 && (
                    <span>{formatEta(merged.eta_seconds)}</span>
                  )}
                  <span className="text-accent-400">polling live · every 3.5s</span>
                </div>
              </motion.div>
            )}

            {merged.status === "failed" && merged.error_message && (
              <div className="mt-5 rounded-xl border border-red-500/20 bg-red-500/[0.07] p-4">
                <p className="text-xs font-medium text-red-200">Extraction failed</p>
                <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-red-200/80">
                  {merged.error_message}
                </pre>
              </div>
            )}
          </GlassCard>

          {/* Facts grid */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Fact label="Records extracted" value={merged.status === "completed" ? formatNumber(merged.record_count ?? 0) : formatNumber(merged.records_processed)} />
            <Fact label="Source size" value={formatBytes(merged.total_bytes)} />
            <Fact label="Output size" value={formatBytes(merged.output_size_bytes)} />
            <Fact label="CSV parts" value={merged.output_file_count != null ? String(merged.output_file_count) : "—"} />
            <Fact label="Progress" value={formatPercent(merged.percent_complete)} />
            <Fact label="Duration" value={formatDuration(merged.duration_seconds)} />
            <Fact label="Created" value={formatDateTime(merged.created_at)} />
            <Fact label="Completed" value={formatDateTime(merged.completed_at)} />
          </div>

          <GlassCard className="p-6" delay={0.05}>
            <h3 className="text-sm font-semibold tracking-tightish">Pipeline stages</h3>
            <StageList stage={merged.current_stage} status={merged.status} />
          </GlassCard>
        </div>
      )}
    </PageTransition>
  );
}

const STAGES = [
  "Building provider lookup",
  "Extracting rates",
  "Enriching NPIs",
  "Packaging",
  "Completed",
];

function StageList({ stage, status }: { stage: string | null; status: Job["status"] }) {
  const idx = STAGES.findIndex((s) => (stage ?? "").toLowerCase().startsWith(s.toLowerCase()));
  const activeIdx = status === "completed" ? STAGES.length - 1 : idx;

  return (
    <ol className="mt-4 space-y-1">
      {STAGES.map((s, i) => {
        const done = status === "completed" || (activeIdx > -1 && i < activeIdx);
        const active = status !== "completed" && i === activeIdx;
        return (
          <li key={s} className="flex items-center gap-3 py-1.5 text-sm">
            <span
              className={`grid h-5 w-5 place-items-center rounded-full text-[10px] ${
                done
                  ? "bg-accent-600/25 text-accent-400"
                  : active
                    ? "bg-amber-500/20 text-amber-300"
                    : "bg-white/[0.05] text-faint"
              }`}
            >
              {done ? "✓" : i + 1}
            </span>
            <span className={done ? "text-dim" : active ? "font-medium text-white" : "text-faint"}>
              {s}
            </span>
            {active && status === "failed" && (
              <span className="text-xs text-red-300">— failed here</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <GlassCard className="p-4">
      <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
      <p className="mt-1.5 text-lg font-semibold tracking-tightish tnum">{value}</p>
    </GlassCard>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <path d="M12 4v10m0 0l-3.5-3.5M12 14l3.5-3.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 17v2a1 1 0 001 1h12a1 1 0 001-1v-2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}
