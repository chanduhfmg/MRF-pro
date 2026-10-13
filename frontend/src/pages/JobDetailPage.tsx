import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { motion } from "framer-motion";
import PageTransition from "../components/PageTransition";
import GlassCard from "../components/GlassCard";
import StatusBadge from "../components/StatusBadge";
import ProgressBar from "../components/ProgressBar";
import Countdown from "../components/Countdown";
import { SkeletonText, Skeleton } from "../components/Skeleton";
import { useAsync } from "../hooks/useAsync";
import { useJobStatusPolling } from "../hooks/useJobStatusPolling";
import { api } from "../lib/api";
import type { Job, JobManifest } from "../lib/types";
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRate,
} from "../lib/format";

export default function JobDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const jobQ = useAsync(() => api.getJob(id), [id]);
  const job = jobQ.data;

  const live = useJobStatusPolling(id, job?.status, () => jobQ.silentRefetch());
  const status = live?.status ?? job?.status;

  // Once complete, the manifest explains the numbers - above all why a run
  // can legitimately end with zero rows. Before that there is nothing to read.
  const manifestQ = useAsync(
    async () => (status === "completed" ? api.getJobManifest(id) : null),
    [id, status],
  );
  // While queued, say what it is waiting behind rather than showing an
  // inert 0% bar - jobs run one at a time.
  const queueQ = useAsync(async () => (status === "queued" ? api.kpis() : null), [id, status]);

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
        downloaded_bytes: live?.downloaded_bytes ?? job.downloaded_bytes,
        throughput_bps: live?.throughput_bps ?? job.throughput_bps,
        cancel_requested: live?.cancel_requested ?? job.cancel_requested,
      }
    : null;

  const running = merged?.status === "queued" || merged?.status === "processing";

  const [stopBusy, setStopBusy] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  async function rerunJob() {
    if (!merged) return;
    setRerunBusy(true);
    setRerunError(null);
    try {
      await api.rerunJob(merged.id);
      jobQ.silentRefetch();
    } catch (e) {
      setRerunError(e instanceof Error ? e.message : String(e));
    } finally {
      setRerunBusy(false);
    }
  }
  async function stopJob() {
    if (!merged) return;
    setStopBusy(true);
    setStopError(null);
    try {
      // Stopping deletes the job - immediately if it was only queued, or
      // within a few seconds once the worker aborts a running one. Either
      // way there's nothing left on this page to show once it's gone, so
      // head back to the list rather than keep polling a vanishing job.
      await api.cancelJob(merged.id);
      navigate("/jobs");
    } catch (e) {
      setStopError(e instanceof Error ? e.message : String(e));
      setStopBusy(false);
    }
  }

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
                {merged.target_type && merged.target_value && (
                  <p className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded-md bg-accent-600/15 px-2 py-0.5 font-medium text-accent-400 tnum">
                      {merged.target_type.toUpperCase()} {merged.target_value}
                    </span>
                    <span className="text-faint">
                      {merged.filter_to_target
                        ? "output narrowed to this provider"
                        : "all provider groups in the file"}
                    </span>
                    {merged.search_id && (
                      <Link
                        to={`/jobs?search=${merged.search_id}`}
                        className="text-accent-400 hover:underline"
                      >
                        see the whole batch →
                      </Link>
                    )}
                  </p>
                )}
              </div>
              {merged.has_output && (
                <div className="flex flex-col items-end gap-1.5">
                  <div className="flex flex-wrap justify-end gap-2">
                    {merged.has_xlsx || (merged.record_count ?? 0) <= 1_000_000 ? (
                      <a
                        href={api.downloadUrl(merged.id, "xlsx")}
                        className="btn btn-primary"
                        title={merged.has_xlsx ? undefined : "Built on first download from the CSV parts"}
                      >
                        <DownloadIcon /> Download Excel
                      </a>
                    ) : (
                      <span
                        className="btn btn-ghost cursor-not-allowed opacity-60"
                        title="This output has more rows than one Excel sheet can hold (1,048,576). Use the CSV zip."
                      >
                        Excel unavailable (too many rows)
                      </span>
                    )}
                    <a
                      href={api.downloadUrl(merged.id, "zip")}
                      className={merged.has_xlsx ? "btn btn-ghost" : "btn btn-primary"}
                      title="CSV parts + manifest.json"
                    >
                      CSV zip
                      <span className="ml-1 text-xs opacity-80">({formatBytes(merged.output_size_bytes)})</span>
                    </a>
                  </div>
                  {merged.download_name && (
                    <span className="max-w-[420px] truncate text-[11px] text-faint tnum" title={merged.download_name}>
                      {merged.download_name}
                    </span>
                  )}
                </div>
              )}
              {running && (
                <div className="flex flex-col items-end gap-1">
                  <button
                    className="btn border border-red-500/30 bg-red-500/10 text-red-200 hover:bg-red-500/20"
                    disabled={stopBusy || merged.cancel_requested}
                    onClick={stopJob}
                    title="Abort this extraction and discard its partial output"
                  >
                    <StopIcon />{" "}
                    {merged.cancel_requested
                      ? "Stopping…"
                      : merged.status === "queued"
                        ? "Remove from queue"
                        : "Stop extraction"}
                  </button>
                  {stopError && <span className="text-[11px] text-red-300">{stopError}</span>}
                </div>
              )}
              {merged.status === "failed" && (
                <div className="flex flex-col items-end gap-1">
                  <button
                    className="btn btn-primary"
                    disabled={rerunBusy}
                    onClick={rerunJob}
                    title="Queue this same file, target and codes again"
                  >
                    <RerunIcon /> {rerunBusy ? "Queuing…" : "Rerun"}
                  </button>
                  {rerunError && <span className="text-[11px] text-red-300">{rerunError}</span>}
                </div>
              )}
            </div>

            {merged.status === "cancelled" && (
              <div className="mt-5 rounded-xl border border-slate-500/25 bg-slate-500/[0.08] p-4 text-xs text-slate-200">
                Stopped by user. Partial output was discarded — nothing to download. Start it
                again from the search results or upload the file to re-run.
              </div>
            )}

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
                  {merged.current_stage?.startsWith("Downloading") ? (
                    <span>
                      {formatBytes(merged.downloaded_bytes)} /{" "}
                      {formatBytes(merged.total_bytes)} downloaded
                    </span>
                  ) : (
                    <span>
                      {formatBytes(merged.bytes_processed)} /{" "}
                      {formatBytes(merged.total_bytes)} read
                    </span>
                  )}
                  {merged.throughput_bps != null && merged.throughput_bps > 0 && (
                    <span>{formatRate(merged.throughput_bps)}</span>
                  )}
                  <Countdown
                    seconds={merged.eta_seconds}
                    active={merged.status === "processing"}
                    className="text-accent-400"
                  />
                  <span className="text-faint">polling live · every 3.5s</span>
                </div>
                {merged.status === "queued" && queueQ.data && (
                  <p className="mt-3 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-200 ring-1 ring-amber-500/20">
                    Waiting for the worker — jobs run one at a time.{" "}
                    {queueQ.data.totals.processing_jobs > 0
                      ? `${formatNumber(queueQ.data.totals.processing_jobs)} running`
                      : "nothing running"}
                    {queueQ.data.totals.queued_jobs > 1
                      ? ` · ${formatNumber(queueQ.data.totals.queued_jobs - 1)} queued ahead of this one`
                      : " · this one is next"}
                    {queueQ.data.queue_eta_seconds != null && (
                      <>
                        {" "}
                        · queue clears in{" "}
                        <Countdown seconds={queueQ.data.queue_eta_seconds} suffix="" />
                      </>
                    )}
                  </p>
                )}
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
            {merged.status === "completed" && merged.matched_code_count != null && (
              <Fact
                label={merged.requested_code_count != null ? "CPT codes matched" : "CPT codes billed by this provider"}
                value={
                  merged.requested_code_count != null
                    ? `${merged.matched_code_count}/${merged.requested_code_count}`
                    : `${merged.matched_code_count}`
                }
                title={
                  merged.matched_codes && merged.matched_codes.length
                    ? `Matched: ${merged.matched_codes.join(", ")}`
                    : "None of the requested codes appeared anywhere in this file"
                }
              />
            )}
            <Fact label="Source size" value={formatBytes(merged.total_bytes)} />
            <Fact label="Output size" value={formatBytes(merged.output_size_bytes)} />
            <Fact label="CSV parts" value={merged.output_file_count != null ? String(merged.output_file_count) : "—"} />
            <Fact label="Progress" value={formatPercent(merged.percent_complete)} />
            <Fact label="Duration" value={formatDuration(merged.duration_seconds)} />
            <Fact label="Created" value={formatDateTime(merged.created_at)} />
            <Fact label="Completed" value={formatDateTime(merged.completed_at)} />
          </div>

          {merged.status === "completed" && manifestQ.data && (
            <ExtractionSummary m={manifestQ.data} job={merged} />
          )}

          <GlassCard className="p-6" delay={0.05}>
            <h3 className="text-sm font-semibold tracking-tightish">Pipeline stages</h3>
            <StageList
              stage={merged.current_stage}
              status={merged.status}
              downloaded={Boolean(merged.source_url)}
            />
          </GlassCard>
        </div>
      )}
    </PageTransition>
  );
}

const BASE_STAGES = [
  "Building provider lookup",
  "Extracting rates",
  "Enriching NPIs",
  "Packaging",
  "Completed",
];

function StageList({
  stage,
  status,
  downloaded,
}: {
  stage: string | null;
  status: Job["status"];
  downloaded: boolean;
}) {
  // Jobs sourced from a search fetch the MRF first; uploaded jobs already
  // have it, so showing them a download step would be a lie.
  const STAGES = downloaded ? ["Downloading MRF", ...BASE_STAGES] : BASE_STAGES;
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

/**
 * What the extractor actually did, from manifest.json. The important case is
 * zero rows: a correct empty result looks identical to a broken run unless
 * the page says which of the user's choices produced it and what to change.
 */
function ExtractionSummary({ m, job }: { m: JobManifest; job: Job }) {
  const es = m.extract_stats ?? {};
  const en = m.enrich_stats ?? {};
  const requested = es.codes_requested ?? [];
  const matched = es.matched_codes ?? [];
  const custom = Boolean(es.custom_codes);
  const notInFile = requested.filter((c) => !matched.includes(c));
  const rows = m.row_count ?? 0;
  const zero = rows === 0;
  const offTarget = es.dropped_off_target ?? 0;
  const corrupt = es.dropped_npi_eq_tin ?? 0;
  const targetLabel = m.target ? `${m.target.type.toUpperCase()} ${m.target.value}` : null;
  const narrowed = Boolean(m.target) && job.filter_to_target;
  const codesLabel = custom
    ? `${requested.length} custom codes`
    : `the default set (${requested.length || 78} codes)`;

  return (
    <GlassCard className={`p-6 ${zero ? "border-amber-500/30" : ""}`} delay={0.03}>
      <h3 className="text-sm font-semibold tracking-tightish">
        {zero ? "Why zero rows?" : "What was extracted"}
      </h3>

      {zero && (
        <div className="mt-3 space-y-2 text-sm leading-relaxed text-dim">
          <p>
            The file was fully scanned ({formatNumber(es.items_scanned ?? 0)} in-network items)
            and the run itself was fine. No row satisfied everything you asked for:
          </p>
          <ul className="list-disc space-y-2 pl-5">
            {narrowed && targetLabel && offTarget > 0 && (
              <li>
                <span className="font-medium text-[var(--text)]">
                  {targetLabel} is in this file, but has no rates for these codes.
                </span>{" "}
                {formatNumber(offTarget)} provider group{offTarget === 1 ? "" : "s"} belonging
                to <em>other</em> providers do bill {matched.length} of them (
                {matched.slice(0, 12).join(", ")}
                {matched.length > 12 ? ", …" : ""}). To get the network's rates for these codes
                instead, re-run and choose <b>Every provider that bills these codes</b>.
              </li>
            )}
            {narrowed && targetLabel && offTarget === 0 && matched.length > 0 && (
              <li>
                {matched.length} of the codes appear in the file, but no provider group carried
                usable rates for them.
              </li>
            )}
            {matched.length === 0 && (
              <li>
                <span className="font-medium text-[var(--text)]">
                  None of the requested codes appear anywhere in this file.
                </span>{" "}
                This network file may not cover these services at all.
              </li>
            )}
            {notInFile.length > 0 && matched.length > 0 && (
              <li>
                {formatNumber(notInFile.length)} of your {formatNumber(requested.length)} codes
                don&rsquo;t appear in this file at all: {notInFile.slice(0, 20).join(", ")}
                {notInFile.length > 20 ? ` … +${notInFile.length - 20} more` : ""}.
              </li>
            )}
            {custom && (
              <li>
                To see what {targetLabel ?? "this provider"} <em>does</em> bill here, re-run with
                the default code set (E&amp;M + ophthalmic) or a broader saved set.
              </li>
            )}
            {corrupt > 0 && (
              <li>
                {formatNumber(corrupt)} provider group{corrupt === 1 ? " was" : "s were"} skipped
                as corrupt payer data (an NPI equal to its own TIN).
              </li>
            )}
          </ul>
        </div>
      )}

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Mini label="Items scanned" value={formatNumber(es.items_scanned ?? 0)} />
        <Mini label="Rows kept" value={formatNumber(rows)} accent={!zero} />
        <Mini
          label={narrowed ? "Other providers' groups dropped" : "Off-target groups dropped"}
          value={formatNumber(offTarget)}
          hint={narrowed ? `not ${targetLabel}` : undefined}
        />
        <Mini label="Corrupt groups skipped" value={formatNumber(corrupt)} hint="NPI equal to own TIN" />
        <Mini label="Codes requested" value={codesLabel} />
        <Mini
          label="Codes found in file"
          value={`${formatNumber(matched.length)} of ${formatNumber(requested.length || 78)}`}
          accent={matched.length > 0}
        />
        <Mini label="Unique NPIs enriched" value={formatNumber(en.unique_npis ?? 0)} />
        <Mini label="Scope" value={narrowed ? `only ${targetLabel}` : "every provider group"} />
      </div>

      {matched.length > 0 && !zero && (
        <p className="mt-4 text-[11px] leading-relaxed text-faint">
          <span className="text-dim">Codes with rates in the output:</span>{" "}
          <span className="tnum">{matched.join(" · ")}</span>
          {notInFile.length > 0 && (
            <>
              <br />
              <span className="text-dim">Requested but not in this file:</span>{" "}
              <span className="tnum">
                {notInFile.slice(0, 30).join(" · ")}
                {notInFile.length > 30 ? ` … +${notInFile.length - 30} more` : ""}
              </span>
            </>
          )}
        </p>
      )}
    </GlassCard>
  );
}

function Mini({
  label,
  value,
  hint,
  accent = false,
}: {
  label: string;
  value: string;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <div className="rounded-xl bg-white/[0.03] px-3.5 py-3 ring-1 ring-white/[0.06]">
      <p className="text-[10px] uppercase tracking-wide text-faint">{label}</p>
      <p className={`mt-1 text-sm font-semibold tracking-tightish tnum ${accent ? "text-accent-400" : ""}`}>
        {value}
      </p>
      {hint && <p className="text-[10px] text-faint">{hint}</p>}
    </div>
  );
}

function Fact({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <GlassCard className="p-4" title={title}>
      <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
      <p className="mt-1.5 text-lg font-semibold tracking-tightish tnum">{value}</p>
    </GlassCard>
  );
}

function RerunIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <path
        d="M4 10a8 8 0 0114.5-4.5M20 14a8 8 0 01-14.5 4.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path d="M18 4v5h-5M6 20v-5h5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
    </svg>
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
