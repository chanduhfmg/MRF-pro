import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import ProgressBar from "../components/ProgressBar";
import EmptyState from "../components/EmptyState";
import StatusBadge from "../components/StatusBadge";
import Countdown from "../components/Countdown";
import PayerIndexPanel from "../components/PayerIndexPanel";
import { useSearchPolling } from "../hooks/useSearchPolling";
import { api, ApiError } from "../lib/api";
import { formatBytes, formatCount, formatDuration, formatNumber } from "../lib/format";
import type {
  DiscoverCodesStatus,
  MatchList,
  Meta,
  ProviderSearch,
  SearchMatch,
  SearchType,
} from "../lib/types";

const FALLBACK_COMPANIES = [
  { key: "uhc", label: "UnitedHealthcare (UHC)" },
  { key: "aetna", label: "Aetna" },
  { key: "anthem", label: "Anthem" },
  { key: "cigna", label: "Cigna Healthcare" },
  { key: "mvp", label: "MVP Health Care" },
];

const PAGE = 10;
const LIVE_MATCH_REFRESH_MS = 3000;

type Phase = "idle" | "searching" | "results" | "error";

/**
 * Provider Lookup: search by NPI/TIN alone (no codes needed up front - this
 * is the discovery tool, not the code-filtered Search page), see which files
 * contain it, then discover every code that provider actually bills in a
 * given file, on demand.
 */
export default function ProviderLookupPage({ meta }: { meta: Meta | null }) {
  const companies = meta?.companies?.length ? meta.companies : FALLBACK_COMPANIES;

  const [company, setCompany] = useState(companies[0]?.key ?? "uhc");
  const [searchType, setSearchType] = useState<SearchType>("npi");
  const [value, setValue] = useState("");
  const [hint, setHint] = useState("");
  const [maxFiles, setMaxFiles] = useState(meta?.search_max_files ?? 40);

  const [phase, setPhase] = useState<Phase>("idle");
  const [search, setSearch] = useState<ProviderSearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  const [matchData, setMatchData] = useState<MatchList | null>(null);
  const [duplicateCount, setDuplicateCount] = useState(0);
  const [page, setPage] = useState(0);
  const pageRef = useRef(0);
  pageRef.current = page;

  // Session-local discovery runs, keyed by match id - layered over each
  // match's own persisted target_codes_* fields the same way the Search
  // page layers cptChecks over cpt_* fields.
  const [discoveries, setDiscoveries] = useState<Record<string, DiscoverCodesStatus>>({});
  const [discoveryStartErrors, setDiscoveryStartErrors] = useState<Record<string, string>>({});
  const discoveryPollTimers = useRef<Record<string, number>>({});

  useEffect(() => {
    const timers = discoveryPollTimers.current;
    return () => {
      Object.values(timers).forEach((t) => window.clearInterval(t));
    };
  }, []);

  const searchId = search?.id ?? null;

  useEffect(() => {
    if (meta?.search_max_files) setMaxFiles(meta.search_max_files);
  }, [meta?.search_max_files]);

  useEffect(() => {
    if (!companies.find((c) => c.key === company)) {
      setCompany(companies[0]?.key ?? "uhc");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta]);

  const refreshMatches = useCallback(
    async (pageIndex: number) => {
      if (!searchId) return;
      try {
        const body = await api.getMatches(searchId, true, PAGE, pageIndex * PAGE);
        setMatchData(body);
        setSearch(body.search);
      } catch {
        /* transient - the next tick will try again */
      }
    },
    [searchId],
  );

  const finalize = useCallback(async () => {
    if (!searchId) return;
    try {
      const [fresh, body, everything] = await Promise.all([
        api.getSearch(searchId),
        api.getMatches(searchId, true, PAGE, pageRef.current * PAGE),
        api.getMatches(searchId, false, 100, 0),
      ]);
      setSearch(fresh);
      setMatchData(body);
      setDuplicateCount(
        everything.matches.filter((m) => m.matched && m.duplicate_of_match_id).length,
      );
      setStopping(false);
      if (fresh.status === "failed") {
        setError(fresh.error_message);
        setPhase("error");
      } else {
        setPhase("results");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [searchId]);

  const live = useSearchPolling(
    phase === "searching" ? searchId : null,
    search?.status,
    finalize,
  );

  useEffect(() => {
    if (phase !== "searching" || !searchId) return;
    const t = window.setInterval(() => refreshMatches(pageRef.current), LIVE_MATCH_REFRESH_MS);
    return () => window.clearInterval(t);
  }, [phase, searchId, refreshMatches]);

  const liveMatched = live?.files_matched;
  useEffect(() => {
    if (phase === "searching" && liveMatched) refreshMatches(pageRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveMatched]);

  const digits = value.replace(/\D/g, "");
  const expected = searchType === "npi" ? 10 : 9;
  const valid = digits.length === expected;

  async function startSearch() {
    setError(null);
    setMatchData(null);
    setDuplicateCount(0);
    setDiscoveries({});
    setDiscoveryStartErrors({});
    Object.values(discoveryPollTimers.current).forEach((t) => window.clearInterval(t));
    discoveryPollTimers.current = {};
    setStopping(false);
    setPage(0);
    setPhase("searching");
    try {
      // No codes: this page matches on TIN/NPI presence alone and discovers
      // codes per file afterward, instead of filtering the search itself.
      const s = await api.createSearch({
        insurance_company: company,
        search_type: searchType,
        search_value: digits,
        hint: hint.trim() || undefined,
        max_files: maxFiles,
      });
      setSearch(s);
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function stopSearch() {
    if (!searchId) return;
    setStopping(true);
    try {
      const s = await api.cancelSearch(searchId);
      setSearch(s);
      if (s.status === "cancelled") finalize();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) finalize();
      else {
        setStopping(false);
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }

  function reset() {
    setPhase("idle");
    setSearch(null);
    setMatchData(null);
    setDuplicateCount(0);
    setError(null);
    setStopping(false);
    setPage(0);
  }

  function goToPage(p: number) {
    setPage(p);
    pageRef.current = p;
    refreshMatches(p);
  }

  function stopDiscoveryPolling(matchId: string) {
    const t = discoveryPollTimers.current[matchId];
    if (t) {
      window.clearInterval(t);
      delete discoveryPollTimers.current[matchId];
    }
  }

  function pollDiscoveryOnce(sid: string, m: SearchMatch) {
    api
      .pollMatchDiscoverCodes(sid, m.id)
      .then((snap) => {
        setDiscoveries((prev) => ({ ...prev, [m.id]: snap }));
        if (snap.status !== "running") stopDiscoveryPolling(m.id);
      })
      .catch(() => {
        /* transient - next tick or manual retry */
      });
  }

  async function discoverCodes(m: SearchMatch) {
    if (!searchId) return;
    setDiscoveryStartErrors((prev) => {
      const { [m.id]: _drop, ...rest } = prev;
      return rest;
    });
    stopDiscoveryPolling(m.id);
    try {
      const snap = await api.startMatchDiscoverCodes(searchId, m.id);
      setDiscoveries((prev) => ({ ...prev, [m.id]: snap }));
      if (snap.status === "running") {
        discoveryPollTimers.current[m.id] = window.setInterval(
          () => pollDiscoveryOnce(searchId, m),
          1000,
        );
      }
    } catch (e) {
      setDiscoveryStartErrors((prev) => ({
        ...prev,
        [m.id]: e instanceof Error ? e.message : String(e),
      }));
    }
  }

  async function stopDiscovery(m: SearchMatch) {
    if (!searchId) return;
    try {
      const snap = await api.cancelMatchDiscoverCodes(searchId, m.id);
      setDiscoveries((prev) => ({ ...prev, [m.id]: snap }));
      stopDiscoveryPolling(m.id);
    } catch {
      /* ignore - poll will pick up the eventual state */
    }
  }

  const searching = phase === "searching";
  const matches = matchData?.matches ?? [];
  const matchedCount = matchData?.matched_count ?? live?.files_matched ?? search?.files_matched ?? 0;
  const scannedCount = matchData?.scanned_count ?? live?.files_scanned ?? search?.files_scanned ?? 0;
  const totalPages = matchData ? Math.max(1, Math.ceil(matchData.total / PAGE)) : 1;
  const stoppedEarly = search?.status === "cancelled";
  const searchingLabel = searchType.toUpperCase();
  const targetLabel = search ? `${search.search_type.toUpperCase()} ${search.search_value}` : null;

  return (
    <PageTransition>
      <PageHeader
        title="Provider Lookup"
        subtitle="Search by NPI or TIN, then discover every code a provider bills in a given file - no code list needed up front."
      />

      <GlassCard className="p-6">
        <label className="mb-2 block text-[13px] font-medium text-dim">Insurance company</label>
        <div className="mb-6 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {companies.map((c) => (
            <button
              key={c.key}
              type="button"
              disabled={searching}
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

        <div className="mb-6">
          <PayerIndexPanel key={company} payerKey={company} compact />
        </div>

        <div className="grid gap-4 lg:grid-cols-[190px_1fr_220px]">
          <div>
            <label className="mb-2 block text-[13px] font-medium text-dim">Search by</label>
            <div className="flex rounded-xl border border-white/10 bg-white/[0.02] p-1">
              {(["npi", "tin"] as SearchType[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  disabled={searching}
                  onClick={() => setSearchType(t)}
                  className={`relative flex-1 rounded-lg px-3 py-2 text-xs font-medium uppercase tracking-wide transition ${
                    searchType === t ? "text-white" : "text-faint hover:text-dim"
                  }`}
                >
                  {searchType === t && (
                    <motion.span
                      layoutId="lookup-type-pill"
                      className="absolute inset-0 rounded-lg bg-accent-600/25 ring-1 ring-accent-400/40"
                      transition={{ type: "spring", stiffness: 320, damping: 28 }}
                    />
                  )}
                  <span className="relative z-10">{t}</span>
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-2 block text-[13px] font-medium text-dim">
              {searchType === "npi" ? "NPI (10 digits)" : "TIN / EIN (9 digits)"}
            </label>
            <input
              className="input tnum"
              inputMode="numeric"
              placeholder={searchType === "npi" ? "1234567893" : "12-3456789"}
              value={value}
              disabled={searching}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && valid && !searching) startSearch();
              }}
            />
            <p className="mt-1.5 text-[11px] text-faint tnum">
              {digits.length}/{expected} digits
              {digits.length > 0 && !valid && (
                <span className="ml-2 text-amber-300">
                  {searchType === "npi" ? "An NPI is 10 digits" : "A TIN is 9 digits"}
                </span>
              )}
            </p>
          </div>

          <div>
            <label className="mb-2 block text-[13px] font-medium text-dim">Files to scan</label>
            <input
              type="number"
              min={1}
              className="input tnum"
              value={maxFiles}
              disabled={searching}
              onChange={(e) => setMaxFiles(Math.max(1, Number(e.target.value) || 1))}
            />
            <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
              How many of the payer&rsquo;s in-network files get opened and scanned for this ID.
            </p>
          </div>
        </div>

        <div className="mt-4">
          <label className="mb-2 block text-[13px] font-medium text-dim">
            Narrow by plan or region <span className="text-faint">(optional)</span>
          </label>
          <input
            className="input"
            placeholder="e.g. Oxford, New York, Choice Plus — only changes which files are scanned first"
            value={hint}
            disabled={searching}
            onChange={(e) => setHint(e.target.value)}
          />
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-3">
          {!searching ? (
            <button className="btn btn-primary" disabled={!valid} onClick={startSearch}>
              <SearchIcon /> Search {searchingLabel}
            </button>
          ) : (
            <button
              className="btn border border-red-500/30 bg-red-500/10 text-red-200 hover:bg-red-500/20"
              disabled={stopping}
              onClick={stopSearch}
            >
              <StopIcon /> {stopping ? "Stopping…" : "Stop process"}
            </button>
          )}
          {(phase === "results" || phase === "error") && (
            <button className="btn btn-ghost" onClick={reset}>
              New lookup
            </button>
          )}
          <span className="text-xs text-faint">
            {searching
              ? "Matches show up below as they're found - you can discover billed codes for one any time."
              : `Scans up to ${formatNumber(maxFiles)} files${
                  meta?.search_concurrency ? `, ${meta.search_concurrency} at a time` : ""
                }.`}
          </span>
        </div>
      </GlassCard>

      <AnimatePresence mode="wait">
        {searching && search && (
          <motion.div
            key="progress"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="mt-6"
          >
            <GlassCard className="p-6">
              <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-sm font-semibold tracking-tightish">
                  Searching {search.insurance_company_label}
                  {stopping && <span className="ml-2 text-xs text-amber-300">stopping…</span>}
                </h3>
                <Countdown
                  seconds={live?.eta_seconds ?? search.eta_seconds}
                  className="text-xs text-accent-400"
                />
              </div>
              <ProgressBar
                percent={live?.percent ?? search.percent_complete}
                stage={live?.stage ?? search.current_stage}
                etaSeconds={live?.eta_seconds ?? search.eta_seconds}
                active
              />
              <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Stat label="Files listed" value={formatNumber(live?.files_listed ?? search.files_listed)} />
                <Stat
                  label="Scanned"
                  value={formatCount(
                    live?.files_scanned ?? search.files_scanned,
                    live?.files_to_scan ?? search.files_to_scan,
                  )}
                />
                <Stat
                  label="Matches"
                  value={formatNumber(live?.files_matched ?? search.files_matched)}
                  accent={(live?.files_matched ?? search.files_matched) > 0}
                />
                <Stat label="Data read" value={formatBytes(live?.bytes_scanned ?? search.bytes_scanned)} />
              </div>
            </GlassCard>
          </motion.div>
        )}
      </AnimatePresence>

      {phase === "error" && error && (
        <GlassCard className="mt-6 border-red-500/20 p-6">
          <p className="text-xs font-medium text-red-200">Search failed</p>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-red-200/80">
            {error.split("\n\nTraceback")[0]}
          </pre>
          <div className="mt-4">
            <PayerIndexPanel payerKey={company} onRetry={startSearch} />
          </div>
        </GlassCard>
      )}

      {search && (searching || phase === "results") && (matchedCount > 0 || phase === "results") && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="mt-6 space-y-5">
          <GlassCard className="p-5">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <h3 className="flex items-center gap-2 text-sm font-semibold tracking-tightish">
                  {matchedCount > 0 ? (
                    <>
                      {formatNumber(matchedCount)} {matchedCount === 1 ? "file contains" : "files contain"}{" "}
                      {targetLabel}
                    </>
                  ) : (
                    <>No files contained {targetLabel}</>
                  )}
                  {searching && (
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-200 ring-1 ring-amber-500/25">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
                      live
                    </span>
                  )}
                  {stoppedEarly && <StatusBadge status="cancelled" size="xs" />}
                </h3>
                <p className="mt-1 text-xs text-dim tnum">
                  {formatNumber(scannedCount)} files scanned
                  {search.files_listed ? ` of ${formatNumber(search.files_listed)} listed` : ""} ·{" "}
                  {formatBytes(live?.bytes_scanned ?? search.bytes_scanned)} read
                  {!searching && search.duration_seconds != null
                    ? ` · ${formatDuration(search.duration_seconds)}`
                    : ""}
                  {stoppedEarly && (
                    <span className="ml-2 text-amber-200">
                      — stopped early, showing what was found up to then
                    </span>
                  )}
                  {duplicateCount > 0 && (
                    <span
                      className="ml-2 text-faint"
                      title="Same TIN, near-identical file size to another match here - almost certainly the same network republished under a second legal entity, not a distinct file. Hidden so you don't download it twice."
                    >
                      — {formatNumber(duplicateCount)} duplicate{duplicateCount === 1 ? "" : "s"} of
                      the same network hidden
                    </span>
                  )}
                </p>
              </div>
            </div>

            {phase === "results" && matchedCount === 0 && (
              <EmptyState
                title={stoppedEarly ? "Stopped before anything matched" : "Nothing matched in the files scanned"}
                hint={
                  search.files_listed > scannedCount
                    ? `Only ${formatNumber(scannedCount)} of ${formatNumber(
                        search.files_listed,
                      )} files were scanned. Raise "Files to scan", or add a plan/region hint, and search again.`
                    : "Every listed file was checked. Double-check the ID, or try the other search type."
                }
              />
            )}
          </GlassCard>

          {matchedCount > 0 && (
            <GlassCard className="overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[900px] text-sm">
                  <thead>
                    <tr className="border-b border-white/[0.06] text-left text-[11px] uppercase tracking-wide text-faint">
                      <th className="px-4 py-3 font-medium">File / plan</th>
                      <th className="px-4 py-3 font-medium">Evidence</th>
                      <th className="px-4 py-3 text-right font-medium">Size</th>
                      <th className="px-4 py-3 text-right font-medium">Est. time</th>
                      <th
                        className="px-4 py-3 pr-5 text-right font-medium"
                        title="Every CPT/HCPCS code the searched NPI/TIN bills in this file - discovered on demand, not a fixed list"
                      >
                        Billed codes
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {matches.map((m) => (
                      <MatchRow
                        key={m.id}
                        match={m}
                        discovery={discoveries[m.id]}
                        discoveryStartError={discoveryStartErrors[m.id]}
                        onDiscover={() => discoverCodes(m)}
                        onStopDiscover={() => stopDiscovery(m)}
                        targetLabel={targetLabel}
                      />
                    ))}
                  </tbody>
                </table>
              </div>

              {totalPages > 1 && (
                <div className="flex items-center justify-between border-t border-white/[0.06] px-4 py-3 text-xs text-faint">
                  <span>
                    Page {page + 1} of {totalPages}
                  </span>
                  <div className="flex gap-2">
                    <button
                      className="btn btn-ghost !px-2.5 !py-1 !text-xs"
                      disabled={page === 0}
                      onClick={() => goToPage(page - 1)}
                    >
                      Prev
                    </button>
                    <button
                      className="btn btn-ghost !px-2.5 !py-1 !text-xs"
                      disabled={page + 1 >= totalPages}
                      onClick={() => goToPage(page + 1)}
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </GlassCard>
          )}
        </motion.div>
      )}
    </PageTransition>
  );
}

/* ---------------- pieces ---------------- */

function MatchRow({
  match,
  discovery,
  discoveryStartError,
  onDiscover,
  onStopDiscover,
  targetLabel,
}: {
  match: SearchMatch;
  /** A run started this session (running, done, cancelled or errored) - takes
   *  priority over the match's own persisted target_codes_* fields, which
   *  may be from an earlier session. */
  discovery?: DiscoverCodesStatus;
  /** Failed to even START the run (bad request, network drop) - distinct
   *  from the run itself erroring mid-scan, which lives on `discovery`. */
  discoveryStartError?: string;
  onDiscover: () => void;
  onStopDiscover: () => void;
  targetLabel?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const running = discovery?.status === "running";

  const effective = discovery && (discovery.status === "done" || discovery.status === "cancelled")
    ? {
        codes: discovery.discovered_codes,
        exhausted: Boolean(discovery.exhausted),
        budgetExceeded: Boolean(discovery.budget_exceeded),
        stopped: discovery.status === "cancelled",
      }
    : match.target_codes_discovered_at
      ? {
          codes: match.target_billed_codes ?? [],
          exhausted: true,
          budgetExceeded: false,
          stopped: false,
        }
      : null;

  return (
    <>
      <tr className="border-b border-white/[0.04] transition-colors hover:bg-white/[0.03]">
        <td className="max-w-[320px] px-4 py-3.5">
          <button
            onClick={() => setOpen((o) => !o)}
            className="block max-w-full truncate text-left font-medium hover:text-accent-400"
            title={match.filename}
          >
            {match.plan_name || match.filename}
          </button>
          <span className="block truncate text-[11px] text-faint" title={match.filename}>
            {match.filename}
          </span>
        </td>
        <td className="px-4 py-3.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`rounded-md px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                match.confidence === "strong"
                  ? "bg-accent-600/20 text-accent-400"
                  : "bg-amber-500/15 text-amber-300"
              }`}
            >
              {match.confidence === "strong" ? "confirmed" : "probable"}
            </span>
            {match.matched_tin && (
              <span className="text-[11px] text-dim tnum">TIN {match.matched_tin}</span>
            )}
            {match.npi_count > 0 && (
              <span className="text-[11px] text-faint tnum">{formatNumber(match.npi_count)} NPIs in group</span>
            )}
          </div>
        </td>
        <td className="px-4 py-3.5 text-right tnum text-dim">{formatBytes(match.file_size_bytes)}</td>
        <td className="px-4 py-3.5 text-right tnum text-dim">
          {match.estimated_seconds ? `~${formatDuration(match.estimated_seconds)}` : "—"}
        </td>
        <td className="px-4 py-3.5 pr-5 text-right">
          {running ? (
            <span className="inline-flex items-center gap-1.5">
              <span className="text-[11px] text-dim">
                {discovery?.phase === "download"
                  ? "Downloading…"
                  : discovery?.phase === "lookup"
                    ? "Resolving providers…"
                    : discovery?.phase === "scan"
                      ? "Reading rates…"
                      : "Discovering…"}{" "}
                <Countdown
                  seconds={discovery?.eta_seconds}
                  active
                  suffix=""
                  className="text-[10px] text-faint"
                />
              </span>
              <button
                className="btn btn-ghost !px-2 !py-0.5 !text-[10px]"
                onClick={onStopDiscover}
                title="Stop this run"
              >
                Stop
              </button>
            </span>
          ) : effective ? (
            <div className="flex flex-col items-end gap-1.5">
              <div className="flex flex-wrap items-center justify-end gap-1.5 text-[11px]">
                <span className="font-medium tnum text-dim">
                  {effective.codes.length} code{effective.codes.length === 1 ? "" : "s"} billed
                </span>
                {effective.stopped && (
                  <span className="text-amber-300" title="Stopped before the file was fully read - this list is a lower bound">
                    stopped
                  </span>
                )}
                {!effective.stopped && effective.budgetExceeded && (
                  <span className="text-amber-300" title="Scan budget reached before the whole file was read - this list is a lower bound">
                    *
                  </span>
                )}
                {(effective.stopped || (!effective.exhausted && !effective.budgetExceeded)) && (
                  <button
                    className="font-normal text-accent-400 hover:underline"
                    onClick={onDiscover}
                    title="Run again"
                  >
                    redo
                  </button>
                )}
              </div>
              {effective.codes.length > 0 ? (
                <div className="flex max-w-[280px] flex-wrap justify-end gap-1">
                  {effective.codes.map((c) => (
                    <span
                      key={c}
                      className="rounded-md bg-accent-600/15 px-1.5 py-0.5 text-[11px] font-medium tnum text-accent-300"
                    >
                      {c}
                    </span>
                  ))}
                </div>
              ) : (
                <span className="text-[10px] text-faint">
                  {targetLabel ?? "This provider"} bills nothing in this file
                </span>
              )}
            </div>
          ) : (
            <button
              className="btn btn-ghost !px-2.5 !py-1 !text-[11px]"
              onClick={onDiscover}
              title={`Downloads this file and resolves provider groups to list every code ${targetLabel ?? "this provider"} bills here - no fixed list, full discovery, so it reads the whole file (no early exit)`}
            >
              Discover billed codes
            </button>
          )}
          {discoveryStartError && (
            <span className="mt-1 block text-[10px] text-red-300" title={discoveryStartError}>
              {discoveryStartError.length > 50 ? discoveryStartError.slice(0, 50) + "…" : discoveryStartError}
            </span>
          )}
          {discovery?.status === "error" && discovery.error && (
            <span className="mt-1 block text-[10px] text-red-300" title={discovery.error}>
              {discovery.error.length > 50 ? discovery.error.slice(0, 50) + "…" : discovery.error}
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr className="border-b border-white/[0.04] bg-white/[0.015]">
          <td colSpan={5} className="px-4 py-3 text-xs text-dim">
            <div className="grid gap-1 sm:grid-cols-2">
              <span>
                <span className="text-faint">Location:</span>{" "}
                <a
                  href={match.location}
                  target="_blank"
                  rel="noreferrer"
                  className="break-all text-accent-400 hover:underline"
                >
                  {match.location}
                </a>
              </span>
              {match.description && (
                <span>
                  <span className="text-faint">Description:</span> {match.description}
                </span>
              )}
              {effective && effective.codes.length > 0 && (
                <span className="sm:col-span-2">
                  <span className="text-faint">Billed codes:</span> {effective.codes.join(", ")}
                </span>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-faint">{label}</div>
      <div className={`mt-0.5 text-lg font-semibold tnum ${accent ? "text-accent-400" : ""}`}>{value}</div>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth="1.8" />
      <path d="M16 16l4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
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
