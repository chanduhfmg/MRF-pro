import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Countdown from "../components/Countdown";
import Select from "../components/Select";
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

const STATUSES: (JobStatus | "")[] = ["", "queued", "processing", "completed", "failed", "cancelled"];
const PAGE = 25;

export default function JobsPage() {
  const navigate = useNavigate();
  const [stopping, setStopping] = useState<Set<string>>(new Set());
  const [rerunning, setRerunning] = useState<Set<string>>(new Set());
  const [rerunErrors, setRerunErrors] = useState<Record<string, string>>({});
  const [params, setParams] = useSearchParams();
  // ?search=<id> shows just the batch a single NPI/TIN search produced, which
  // is where the Search page sends you after queueing several files at once.
  const searchId = params.get("search");
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
  const jobsQ = useAsync(
    async () => {
      if (searchId) {
        const jobs = await api.jobsForSearch(searchId);
        return { jobs, total: jobs.length, limit: jobs.length, offset: 0 };
      }
      return api.listJobs(filters);
    },
    [JSON.stringify(filters), searchId],
  );

  const total = jobsQ.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const anyFilter = company || status || from || to;

  // While anything is running, refresh quietly so the countdowns re-sync
  // with the worker's real progress instead of drifting on a stale number.
  const anyRunning = (jobsQ.data?.jobs ?? []).some(
    (j) => j.status === "processing" || j.status === "queued",
  );
  useEffect(() => {
    if (!anyRunning) return;
    const t = window.setInterval(() => jobsQ.silentRefetch(), 5000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyRunning, JSON.stringify(filters), searchId]);

  async function stopJob(id: string) {
    setStopping((s) => new Set(s).add(id));
    try {
      await api.cancelJob(id);
      jobsQ.silentRefetch();
    } catch {
      /* the row keeps its current state; the detail page shows the reason */
    } finally {
      setStopping((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      });
    }
  }

  async function rerunJob(id: string) {
    setRerunning((s) => new Set(s).add(id));
    setRerunErrors((e) => {
      const { [id]: _drop, ...rest } = e;
      return rest;
    });
    try {
      await api.rerunJob(id);
      jobsQ.silentRefetch();
    } catch (e) {
      setRerunErrors((prev) => ({
        ...prev,
        [id]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setRerunning((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      });
    }
  }

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

      {searchId && (
        <GlassCard className="mb-5 flex flex-wrap items-center justify-between gap-3 p-4">
          <span className="text-xs text-dim">
            Showing only the jobs queued from one NPI/TIN search.
          </span>
          <button
            className="btn btn-ghost !px-3 !py-1.5 !text-xs"
            onClick={() => setParams({})}
          >
            Show all jobs
          </button>
        </GlassCard>
      )}

      {/* Filters */}
      <GlassCard className={`mb-5 p-4 ${searchId ? "hidden" : ""}`} delay={0}>
        <div className="flex flex-wrap items-end gap-3">
          <Filter label="Company">
            <Select
              ariaLabel="Filter by insurance company"
              value={company}
              onChange={(v) => {
                setPage(0);
                setCompany(v);
              }}
              options={[
                { value: "", label: "All" },
                ...(companiesQ.data ?? []).map((c) => ({ value: c.key, label: c.label })),
              ]}
            />
          </Filter>
          <Filter label="Status">
            <Select
              ariaLabel="Filter by job status"
              value={status}
              onChange={(v) => {
                setPage(0);
                setStatus(v as JobStatus | "");
              }}
              options={STATUSES.map((s) => ({
                value: s,
                label: s ? s[0].toUpperCase() + s.slice(1) : "All",
              }))}
            />
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
                <Th className="text-right">Duration / ETA</Th>
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
                      {j.status === "completed" ? (
                        <>
                          {formatNumber(j.record_count ?? 0)}
                          {j.matched_code_count != null && (
                            <span
                              className="mt-0.5 block text-[10px] font-normal text-faint"
                              title={
                                j.matched_codes && j.matched_codes.length
                                  ? `Codes matched: ${j.matched_codes.join(", ")}`
                                  : "None of the requested codes appeared in this file"
                              }
                            >
                              {j.requested_code_count != null
                                ? `${j.matched_code_count}/${j.requested_code_count} codes matched`
                                : `${j.matched_code_count} code${j.matched_code_count === 1 ? "" : "s"} billed by this provider`}
                            </span>
                          )}
                        </>
                      ) : j.status === "processing" ? (
                        formatNumber(j.records_processed)
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-4 py-3.5 text-right tnum text-dim">
                      {formatBytes(j.output_size_bytes)}
                    </td>
                    <td className="px-4 py-3.5 text-right tnum">
                      {j.status === "processing" ? (
                        <Countdown seconds={j.eta_seconds} className="text-accent-400" />
                      ) : j.status === "queued" ? (
                        <span className="text-faint">waiting</span>
                      ) : (
                        <span className="text-dim">{formatDuration(j.duration_seconds)}</span>
                      )}
                    </td>
                    <td className="px-4 py-3.5 text-right text-faint" title={j.created_at}>
                      {formatRelative(j.created_at)}
                    </td>
                    <td className="px-4 py-3.5 pr-5 text-right">
                      {j.has_output ? (
                        <span className="inline-flex gap-1.5" title={j.download_name ?? undefined}>
                          {(j.has_xlsx || (j.record_count ?? 0) <= 1_000_000) && (
                            <a
                              href={api.downloadUrl(j.id, "xlsx")}
                              onClick={(e) => e.stopPropagation()}
                              className="btn btn-primary !px-2.5 !py-1.5 !text-xs"
                            >
                              <DownloadIcon /> Excel
                            </a>
                          )}
                          <a
                            href={api.downloadUrl(j.id, "zip")}
                            onClick={(e) => e.stopPropagation()}
                            className="btn btn-ghost !px-2.5 !py-1.5 !text-xs"
                          >
                            Zip
                          </a>
                        </span>
                      ) : j.status === "queued" || j.status === "processing" ? (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            stopJob(j.id);
                          }}
                          disabled={stopping.has(j.id) || j.cancel_requested}
                          className="btn !px-2.5 !py-1.5 !text-xs border border-red-500/30 bg-red-500/10 text-red-200 hover:bg-red-500/20"
                          title={j.status === "queued" ? "Remove from queue" : "Stop this extraction"}
                        >
                          {j.cancel_requested || stopping.has(j.id) ? "Stopping…" : "Stop"}
                        </button>
                      ) : j.status === "failed" ? (
                        <span className="inline-flex flex-col items-end gap-1">
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              rerunJob(j.id);
                            }}
                            disabled={rerunning.has(j.id)}
                            className="btn btn-ghost !px-2.5 !py-1.5 !text-xs"
                            title={j.error_message ?? "Retry this extraction"}
                          >
                            <RerunIcon /> {rerunning.has(j.id) ? "Queuing…" : "Rerun"}
                          </button>
                          {rerunErrors[j.id] && (
                            <span className="max-w-[180px] truncate text-[10px] text-red-300" title={rerunErrors[j.id]}>
                              {rerunErrors[j.id]}
                            </span>
                          )}
                        </span>
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

function RerunIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
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
function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
      <path d="M12 4v10m0 0l-3.5-3.5M12 14l3.5-3.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 17v2a1 1 0 001 1h12a1 1 0 001-1v-2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}
