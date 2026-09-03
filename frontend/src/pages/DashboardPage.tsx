import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
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

  return (
    <PageTransition>
      <PageHeader
        title="KPI Dashboard"
        subtitle="Live aggregates across every extraction job."
        actions={
          <Link to="/upload" className="btn btn-primary">
            New extraction
          </Link>
        }
      />

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
