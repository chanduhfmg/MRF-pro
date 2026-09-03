import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import { Skeleton } from "../components/Skeleton";
import { useAsync } from "../hooks/useAsync";
import { api, type JobFilters } from "../lib/api";
import type { JobStatus } from "../lib/types";
import {
  formatBytes,
  formatDuration,
  formatNumber,
  formatRelative,
} from "../lib/format";

const STATUSES: (JobStatus | "")[] = ["", "queued", "processing", "completed", "failed"];
const PAGE = 25;

export default function JobsPage() {
  const navigate = useNavigate();
  const [company, setCompany] = useState("");
  const [status, setStatus] = useState<JobStatus | "">("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(0);

  const companiesQ = useAsync(() => api.companies(), []);
  const filters: JobFilters = useMemo(
    () => ({
      company: company || undefined,
      status: status || undefined,
      date_from: from || undefined,
      date_to: to || undefined,
      limit: PAGE,
      offset: page * PAGE,
    }),
    [company, status, from, to, page],
  );
  const jobsQ = useAsync(() => api.listJobs(filters), [JSON.stringify(filters)]);

  const total = jobsQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const anyFilter = company || status || from || to;

  function resetFilters() {
    setCompany("");
    setStatus("");
    setFrom("");
    setTo("");
    setPage(0);
  }

  return (
    <PageTransition>
      <PageHeader
        title="Jobs"
        subtitle={`${formatNumber(total)} extraction ${total === 1 ? "job" : "jobs"}`}
        actions={
          <Link to="/upload" className="btn btn-primary">
            New extraction
          </Link>
        }
      />

      {/* Filters */}
      <GlassCard className="mb-5 p-4" delay={0}>
        <div className="flex flex-wrap items-end gap-3">
          <Filter label="Company">
            <select
              className="input"
              value={company}
              onChange={(e) => {
                setPage(0);
                setCompany(e.target.value);
              }}
            >
              <option value="">All</option>
              {(companiesQ.data ?? []).map((c) => (
                <option key={c.key} value={c.key}>
                  {c.label}
                </option>
              ))}
            </select>
          </Filter>
          <Filter label="Status">
            <select
              className="input"
              value={status}
              onChange={(e) => {
                setPage(0);
                setStatus(e.target.value as JobStatus | "");
              }}
            >
              {STATUSES.map((s) => (
                <option key={s || "all"} value={s}>
                  {s ? s[0].toUpperCase() + s.slice(1) : "All"}
                </option>
              ))}
            </select>
          </Filter>
          <Filter label="From">
            <input
              type="date"
              className="input"
              value={from}
              onChange={(e) => {
                setPage(0);
                setFrom(e.target.value);
              }}
            />
          </Filter>
          <Filter label="To">
            <input
              type="date"
              className="input"
              value={to}
              onChange={(e) => {
                setPage(0);
                setTo(e.target.value);
              }}
            />
          </Filter>
          {anyFilter && (
            <button className="btn btn-ghost h-[42px]" onClick={resetFilters}>
              Clear
            </button>
          )}
        </div>
      </GlassCard>

      {/* Table */}
      <GlassCard className="overflow-hidden" delay={0.05}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] text-sm">
            <thead>
              <tr className="border-b border-white/[0.06] text-left text-[11px] uppercase tracking-wide text-faint">
                <Th>Company</Th>
                <Th>File</Th>
                <Th>Status</Th>
                <Th className="text-right">Records</Th>
                <Th className="text-right">Size</Th>
                <Th className="text-right">Duration</Th>
                <Th className="text-right">Created</Th>
                <Th className="text-right pr-5">Output</Th>
              </tr>
            </thead>
            <tbody>
              {jobsQ.loading &&
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-b border-white/[0.04]">
                    {Array.from({ length: 8 }).map((_, j) => (
                      <td key={j} className="px-4 py-3.5">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))}

              {!jobsQ.loading &&
                (jobsQ.data?.jobs ?? []).map((j) => (
                  <tr
                    key={j.id}
                    onClick={() => navigate(`/jobs/${j.id}`)}
                    className="group cursor-pointer border-b border-white/[0.04] transition-colors hover:bg-white/[0.03]"
                  >
                    <td className="px-4 py-3.5">
                      <span className="font-medium">{j.insurance_company_label.replace(/\s*\(.*\)/, "")}</span>
                    </td>
                    <td className="max-w-[240px] px-4 py-3.5">
                      <span className="block truncate text-dim">{j.source_filename}</span>
                    </td>
                    <td className="px-4 py-3.5">
                      <StatusBadge status={j.status} size="xs" />
                    </td>
                    <td className="px-4 py-3.5 text-right tnum text-dim">
                      {j.status === "completed"
                        ? formatNumber(j.record_count ?? 0)
                        : j.status === "processing"
                          ? formatNumber(j.records_processed)
                          : "—"}
                    </td>
                    <td className="px-4 py-3.5 text-right tnum text-dim">
                      {formatBytes(j.output_size_bytes)}
                    </td>
                    <td className="px-4 py-3.5 text-right tnum text-dim">
                      {formatDuration(j.duration_seconds)}
                    </td>
                    <td className="px-4 py-3.5 text-right text-faint" title={j.created_at}>
                      {formatRelative(j.created_at)}
                    </td>
                    <td className="px-4 py-3.5 pr-5 text-right">
                      {j.has_output ? (
                        <a
                          href={api.downloadUrl(j.id)}
                          onClick={(e) => e.stopPropagation()}
                          className="btn btn-ghost !px-2.5 !py-1.5 !text-xs"
                        >
                          <DownloadIcon /> Zip
                        </a>
                      ) : (
                        <span className="text-xs text-faint">—</span>
                      )}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>

        {!jobsQ.loading && !jobsQ.error && (jobsQ.data?.jobs.length ?? 0) === 0 && (
          <EmptyState
            title={anyFilter ? "No jobs match these filters" : "No jobs yet"}
            hint={anyFilter ? "Try widening the date range or clearing filters." : "Upload an MRF to kick off the first extraction."}
            action={
              anyFilter ? (
                <button className="btn btn-ghost" onClick={resetFilters}>
                  Clear filters
                </button>
              ) : (
                <Link to="/upload" className="btn btn-primary">
                  Upload MRF
                </Link>
              )
            }
          />
        )}

        {jobsQ.error && (
          <div className="px-4 py-10 text-center text-sm text-red-300">{jobsQ.error}</div>
        )}

        {pages > 1 && (
          <div className="flex items-center justify-between border-t border-white/[0.06] px-4 py-3 text-xs text-faint">
            <span className="tnum">
              Page {page + 1} of {pages}
            </span>
            <div className="flex gap-2">
              <button
                className="btn btn-ghost !px-3 !py-1.5"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                Prev
              </button>
              <button
                className="btn btn-ghost !px-3 !py-1.5"
                disabled={page + 1 >= pages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </div>
        )}
      </GlassCard>
    </PageTransition>
  );
}

function Filter({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11px] uppercase tracking-wide text-faint">{label}</span>
      <div className="w-[170px]">{children}</div>
    </label>
  );
}

function Th({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <th className={`px-4 py-3 font-medium ${className}`}>{children}</th>;
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
      <path d="M12 4v10m0 0l-3.5-3.5M12 14l3.5-3.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 17v2a1 1 0 001 1h12a1 1 0 001-1v-2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}
