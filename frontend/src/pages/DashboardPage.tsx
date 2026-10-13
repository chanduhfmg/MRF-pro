import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Countdown from "../components/Countdown";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import KpiCard from "../components/KpiCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import { Skeleton } from "../components/Skeleton";
import { ChartFrame } from "../components/charts/ChartFrame";
import JobsByCompanyChart from "../components/charts/JobsByCompanyChart";
import VolumeChart from "../components/charts/VolumeChart";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import {
  formatBytes,
  formatCompact,
  formatNumber,
  formatRelative,
} from "../lib/format";

export default function DashboardPage() {
  const navigate = useNavigate();
  const q = useAsync(() => api.kpis(), []);
  const [metric, setMetric] = useState<"records" | "jobs">("records");
  const k = q.data;
  const loading = q.loading;

  // keep the queue countdown honest while jobs are running
  const busy = Boolean(k && (k.totals.processing_jobs > 0 || k.totals.queued_jobs > 0));
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => q.silentRefetch(), 8000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  // Searches never show up in /api/kpis - that endpoint only ever aggregates
  // extraction jobs, so a search that hasn't produced any jobs yet (or
  // won't, if nothing matched) was otherwise invisible outside the Search
  // page itself. Pull "processing" and "queued" separately rather than one
  // unfiltered call, so an active search is never pushed off the page by
  // older completed ones ahead of it.
  const activeSearchesQ = useAsync(async () => {
    const [processing, queued] = await Promise.all([
      api.listSearches(10, "processing"),
      api.listSearches(10, "queued"),
    ]);
    return [...processing.searches, ...queued.searches];
  }, []);
  const activeSearches = activeSearchesQ.data ?? [];
  useEffect(() => {
    if (activeSearches.length === 0) return;
    const t = window.setInterval(() => activeSearchesQ.silentRefetch(), 5000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSearches.length]);

  // Same gap as searches, but for jobs: the KPI totals say "3 processing"
  // with no way to see which three, what file each one is on, or - once one
  // finishes - a direct way to grab it without digging through the full
  // Jobs page and its filters. List the actual rows here instead.
  const activeJobsQ = useAsync(async () => {
    const [processing, queued] = await Promise.all([
      api.listJobs({ status: "processing", limit: 10 }),
      api.listJobs({ status: "queued", limit: 10 }),
    ]);
    return [...processing.jobs, ...queued.jobs];
  }, []);
  const activeJobs = activeJobsQ.data ?? [];

  // Finished recently and ready to grab - the thing that's otherwise easy to
  // lose track of once a few more jobs queue up behind it.
  const readyJobsQ = useAsync(
    () => api.listJobs({ status: "completed", limit: 5 }),
    [],
  );
  const readyJobs = (readyJobsQ.data?.jobs ?? []).filter((j) => j.has_output);

  useEffect(() => {
    if (activeJobs.length === 0) return;
    const t = window.setInterval(() => {
      activeJobsQ.silentRefetch();
      readyJobsQ.silentRefetch();
    }, 5000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJobs.length]);

  return (
    <PageTransition>
      <PageHeader
        title="KPI Dashboard"
        subtitle="Live aggregates across every extraction job."
        actions={
          <Link to="/search" className="btn btn-primary">
            New search
          </Link>
        }
      />

      {/* Active searches - invisible everywhere else on this page, since
          /api/kpis only ever aggregates jobs. A search that hasn't produced
          any jobs yet (or never will, if nothing matched) otherwise looked
          like nothing was happening. */}
      {activeSearches.length > 0 && (
        <GlassCard className="mb-5 p-4">
          <div className="mb-3 flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-400" />
            </span>
            <h3 className="text-sm font-medium">
              {activeSearches.length} active search{activeSearches.length === 1 ? "" : "es"}
            </h3>
          </div>
          <div className="space-y-2.5">
            {activeSearches.map((s) => (
              <Link
                key={s.id}
                to={`/search?resume=${s.id}`}
                title="Open the files matched so far - available to download/extract without waiting for the scan to finish"
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 transition-colors hover:border-accent-500/30 hover:bg-white/[0.04]"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <StatusBadge status={s.status} size="xs" />
                  <span className="truncate text-sm">
                    <span className="font-medium">{s.insurance_company_label}</span>
                    <span className="text-faint">
                      {" "}
                      · {s.search_type.toUpperCase()} {s.search_value}
                    </span>
                  </span>
                </div>
                <div className="flex items-center gap-3 text-xs text-dim tnum">
                  <span>{s.current_stage || "Queued"}</span>
                  {s.status === "processing" && (
                    <>
                      <span>
                        {formatNumber(s.files_scanned)}
                        {s.files_listed ? ` / ${formatNumber(s.files_listed)}` : ""} files
                      </span>
                      <span className="text-emerald-300">{formatNumber(s.files_matched)} matched</span>
                      {s.eta_seconds != null && (
                        <Countdown seconds={s.eta_seconds} suffix=" left" />
                      )}
                    </>
                  )}
                </div>
              </Link>
            ))}
          </div>
        </GlassCard>
      )}

      {/* Active jobs - which files are actually being worked on right now,
          not just a count. */}
      {activeJobs.length > 0 && (
        <GlassCard className="mb-5 p-4">
          <div className="mb-3 flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent-400 opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent-400" />
            </span>
            <h3 className="text-sm font-medium">
              {activeJobs.length} active job{activeJobs.length === 1 ? "" : "s"}
            </h3>
          </div>
          <div className="space-y-2.5">
            {activeJobs.map((j) => (
              <div
                key={j.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <StatusBadge status={j.status} size="xs" />
                  <Link
                    to={`/jobs/${j.id}`}
                    className="truncate text-sm hover:text-accent-300"
                    title={j.source_filename}
                  >
                    <span className="font-medium">{j.insurance_company_label}</span>
                    <span className="text-faint"> · {j.source_filename}</span>
                  </Link>
                </div>
                <div className="flex items-center gap-3 text-xs text-dim tnum">
                  <span>{j.current_stage || "Queued"}</span>
                  {j.status === "processing" && (
                    <>
                      <span>{j.percent_complete.toFixed(0)}%</span>
                      {j.eta_seconds != null && (
                        <Countdown seconds={j.eta_seconds} suffix=" left" />
                      )}
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        </GlassCard>
      )}

      {/* Ready to download - easy to lose track of once more jobs queue up
          behind a finished one. */}
      {readyJobs.length > 0 && (
        <GlassCard className="mb-5 p-4">
          <h3 className="mb-3 text-sm font-medium">Ready to download</h3>
          <div className="space-y-2.5">
            {readyJobs.map((j) => (
              <div
                key={j.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2"
              >
                <Link
                  to={`/jobs/${j.id}`}
                  className="min-w-0 truncate text-sm hover:text-accent-300"
                  title={j.source_filename}
                >
                  <span className="font-medium">{j.insurance_company_label}</span>
                  <span className="text-faint"> · {j.source_filename}</span>
                </Link>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-faint tnum">
                    {formatRelative(j.completed_at)}
                  </span>
                  <a
                    className="btn btn-ghost !px-2 !py-1 !text-[11px]"
                    href={api.downloadUrl(j.id, "xlsx")}
                    title={j.download_name ?? undefined}
                  >
                    <DownloadIcon /> Excel
                  </a>
                  <a
                    className="btn btn-ghost !px-2 !py-1 !text-[11px]"
                    href={api.downloadUrl(j.id, "zip")}
                  >
                    <DownloadIcon /> Zip
                  </a>
                </div>
              </div>
            ))}
          </div>
        </GlassCard>
      )}

      {/* Queue drain ETA - only meaningful while something is actually in flight */}
      {k && k.queue_eta_seconds != null && (
        <GlassCard className="mb-5 flex flex-wrap items-center justify-between gap-3 p-4">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent-400 opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent-400" />
            </span>
            <span className="text-sm">
              <span className="font-medium">
                {formatNumber(k.totals.processing_jobs)} running
              </span>
              <span className="text-faint">
                {" "}
                · {formatNumber(k.totals.queued_jobs)} queued
              </span>
            </span>
          </div>
          <span className="text-sm tnum">
            <span className="text-faint">queue clears in </span>
            <Countdown
              seconds={k.queue_eta_seconds}
              suffix=""
              className="font-semibold text-accent-400"
            />
          </span>
        </GlassCard>
      )}

      {q.error && (
        <GlassCard className="p-8 text-center text-sm text-red-300">{q.error}</GlassCard>
      )}

      {/* KPI cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          label="Total jobs"
          value={k?.totals.total_jobs ?? 0}
          loading={loading}
          delay={0}
          icon={<DotIcon />}
          sub={
            k
              ? `${k.totals.processing_jobs} running · ${k.totals.queued_jobs} queued`
              : undefined
          }
        />
        <KpiCard
          label="Records extracted"
          value={k?.totals.total_records ?? 0}
          format={(n) => formatCompact(n)}
          loading={loading}
          delay={0.05}
          accent
          icon={<RowsIcon />}
          sub={k ? `${formatNumber(k.totals.total_records)} total` : undefined}
        />
        <KpiCard
          label="Storage used"
          value={k?.totals.total_storage_bytes ?? 0}
          format={(n) => formatBytes(n)}
          loading={loading}
          delay={0.1}
          icon={<DiskIcon />}
          sub="across packaged output zips"
        />
        <KpiCard
          label="Success rate"
          value={(k?.totals.success_rate ?? 0) * 100}
          format={(n) => `${n.toFixed(n >= 99.95 || n === 0 ? 0 : 1)}%`}
          loading={loading}
          delay={0.15}
          icon={<CheckIcon />}
          sub={
            k
              ? `${k.totals.completed_jobs} completed · ${k.totals.failed_jobs} failed`
              : undefined
          }
        />
      </div>

      {/* Charts */}
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <GlassCard className="h-[300px]" delay={0.1}>
          <ChartFrame title="Jobs per insurance company" subtitle="All-time count">
            {loading ? (
              <Skeleton className="h-full w-full" />
            ) : (
              <JobsByCompanyChart data={k?.by_company ?? []} />
            )}
          </ChartFrame>
        </GlassCard>

        <GlassCard className="h-[300px]" delay={0.15}>
          <ChartFrame
            title="Extraction volume over time"
            subtitle="Last 30 days"
            right={
              <div className="flex rounded-lg bg-white/[0.04] p-0.5 text-[11px] ring-1 ring-white/[0.06]">
                {(["records", "jobs"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setMetric(m)}
                    className={`rounded-md px-2.5 py-1 capitalize transition ${
                      metric === m ? "bg-white/10 text-white" : "text-faint hover:text-white"
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            }
          >
            {loading ? (
              <Skeleton className="h-full w-full" />
            ) : (
              <VolumeChart data={k?.volume_over_time ?? []} metric={metric} />
            )}
          </ChartFrame>
        </GlassCard>
      </div>

      {/* Recent jobs */}
      <GlassCard className="mt-6 overflow-hidden" delay={0.2}>
        <div className="flex items-center justify-between px-5 py-4">
          <h3 className="text-sm font-semibold tracking-tightish">Recent jobs</h3>
          <Link to="/jobs" className="text-xs text-dim hover:text-white">
            View all →
          </Link>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <tbody>
              {loading &&
                Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i} className="border-t border-white/[0.04]">
                    {Array.from({ length: 4 }).map((_, j) => (
                      <td key={j} className="px-5 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))}
              {!loading &&
                (k?.recent_jobs ?? []).map((j) => (
                  <tr
                    key={j.id}
                    onClick={() => navigate(`/jobs/${j.id}`)}
                    className="cursor-pointer border-t border-white/[0.04] transition-colors hover:bg-white/[0.03]"
                  >
                    <td className="px-5 py-3 font-medium">
                      {j.insurance_company_label.replace(/\s*\(.*\)/, "")}
                    </td>
                    <td className="max-w-[260px] px-5 py-3">
                      <span className="block truncate text-dim">{j.source_filename}</span>
                    </td>
                    <td className="px-5 py-3">
                      <StatusBadge status={j.status} size="xs" />
                    </td>
                    <td className="px-5 py-3 text-right tnum text-dim">
                      {j.status === "completed" ? formatNumber(j.record_count ?? 0) : "—"}
                    </td>
                    <td className="px-5 py-3 text-right text-faint">{formatRelative(j.created_at)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        {!loading && (k?.recent_jobs.length ?? 0) === 0 && (
          <EmptyState
            title="No jobs yet"
            hint="Once you run an extraction it shows up here."
            action={
              <Link to="/upload" className="btn btn-primary">
                Upload MRF
              </Link>
            }
          />
        )}
      </GlassCard>
    </PageTransition>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
      <path d="M12 4v10m0 0l-3.5-3.5M12 14l3.5-3.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 17v2a1 1 0 001 1h12a1 1 0 001-1v-2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}
function DotIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <circle cx="12" cy="12" r="7" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}
function RowsIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <rect x="4" y="5" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.6" />
      <rect x="4" y="12" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.6" opacity="0.6" />
    </svg>
  );
}
function DiskIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <ellipse cx="12" cy="6" rx="7" ry="3" stroke="currentColor" strokeWidth="1.6" />
      <path d="M5 6v12c0 1.66 3.13 3 7 3s7-1.34 7-3V6" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <path d="M5 13l4 4L19 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
