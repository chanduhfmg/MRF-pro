import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import ProgressBar from "../components/ProgressBar";
import EmptyState from "../components/EmptyState";
import StatusBadge from "../components/StatusBadge";
import Countdown from "../components/Countdown";
import CodeSetPanel, { type CodeSelection } from "../components/CodeSetPanel";
import PayerIndexPanel from "../components/PayerIndexPanel";
import ExtractScopeDialog, { type ExtractScope } from "../components/ExtractScopeDialog";
import { useSearchPolling } from "../hooks/useSearchPolling";
import { api, ApiError } from "../lib/api";
import {
  formatBytes,
  formatCount,
  formatDuration,
  formatNumber,
} from "../lib/format";
import type {
  CptCheckStatus,
  Job,
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
// how often the match list refreshes while the search is still running
const LIVE_MATCH_REFRESH_MS = 3000;

type Phase = "idle" | "searching" | "results" | "error";
type RowState = { busy?: boolean; error?: string | null };

export default function SearchPage({ meta }: { meta: Meta | null }) {
  const companies = meta?.companies?.length ? meta.companies : FALLBACK_COMPANIES;
  const [searchParams] = useSearchParams();
  const resumeId = searchParams.get("resume");

  // ---- form ---------------------------------------------------------------
  const [company, setCompany] = useState(companies[0]?.key ?? "uhc");
  const [searchType, setSearchType] = useState<SearchType>("npi");
  const [value, setValue] = useState("");
  const [hint, setHint] = useState("");
  const [hintStrict, setHintStrict] = useState(false);
  const [maxFiles, setMaxFiles] = useState(meta?.search_max_files ?? 40);

  // ---- search lifecycle ---------------------------------------------------
  const [phase, setPhase] = useState<Phase>("idle");
  const [search, setSearch] = useState<ProviderSearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  // ---- results ------------------------------------------------------------
  const [matchData, setMatchData] = useState<MatchList | null>(null);
  const [page, setPage] = useState(0);
  const pageRef = useRef(0);
  pageRef.current = page;
  const [ruledOut, setRuledOut] = useState<SearchMatch[]>([]);
  const [duplicateCount, setDuplicateCount] = useState(0);

  // ---- extraction ---------------------------------------------------------
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filterToTarget, setFilterToTarget] = useState(true);
  const [codeSel, setCodeSel] = useState<CodeSelection>({ codes: [], codeSetId: null });
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [rows, setRows] = useState<Record<string, RowState>>({});
  // Session-local CPT coverage checks. Layered over each match's own
  // persisted cpt_* fields (from a previous check, possibly a previous
  // session) so a check made just now shows up immediately without waiting
  // for the matches list to refetch. `status` doubles as the busy flag
  // ("running") so there's one source of truth instead of a separate
  // busy/error map that can drift out of sync with it.
  const [cptChecks, setCptChecks] = useState<Record<string, CptCheckStatus>>({});
  const [cptStartErrors, setCptStartErrors] = useState<Record<string, string>>({});
  const cptPollTimers = useRef<Record<string, number>>({});

  useEffect(() => {
    const timers = cptPollTimers.current;
    return () => {
      Object.values(timers).forEach((t) => window.clearInterval(t));
    };
  }, []);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [queuedJobs, setQueuedJobs] = useState<Job[]>([]);

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

  // ---- data loading -------------------------------------------------------

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
      const [fresh, body, ruled] = await Promise.all([
        api.getSearch(searchId),
        api.getMatches(searchId, true, PAGE, pageRef.current * PAGE),
        api.getMatches(searchId, false, 100, 0),
      ]);
      setSearch(fresh);
      setMatchData(body);
      setRuledOut(ruled.matches.filter((m) => !m.matched));
      setDuplicateCount(ruled.matches.filter((m) => m.matched && m.duplicate_of_match_id).length);
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

  // Matches appear while the scan is still running - keep the list fresh so
  // a hit can be extracted the moment it lands, not when the whole scan ends.
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

  // Resuming an existing search from elsewhere (the Dashboard's active-tasks
  // list links here as /search?resume=<id>) - a search that's still running
  // in the background, or one that already finished, is otherwise only
  // reachable from the tab that started it. Inlined rather than reusing
  // finalize()/refreshMatches(): those are useCallbacks closed over the
  // searchId state they themselves set, so calling them in the same tick as
  // the setSearch() that creates that id would still be working with last
  // render's (null) closure. The polling/live-refresh effects above pick up
  // normally once phase/searchId are set here, same as a freshly-started one.
  useEffect(() => {
    if (!resumeId) return;
    let cancelled = false;
    (async () => {
      setError(null);
      setMatchData(null);
      setRuledOut([]);
      setDuplicateCount(0);
      setSelected(new Set());
      setRows({});
      setQueuedJobs([]);
      setBulkError(null);
      setStopping(false);
      setPage(0);
      pageRef.current = 0;
      try {
        const fresh = await api.getSearch(resumeId);
        if (cancelled) return;
        setSearch(fresh);
        if (fresh.status === "queued" || fresh.status === "processing") {
          setPhase("searching");
          const body = await api.getMatches(resumeId, true, PAGE, 0);
          if (!cancelled) setMatchData(body);
        } else {
          const [body, ruled] = await Promise.all([
            api.getMatches(resumeId, true, PAGE, 0),
            api.getMatches(resumeId, false, 100, 0),
          ]);
          if (cancelled) return;
          setMatchData(body);
          setRuledOut(ruled.matches.filter((m) => !m.matched));
          setDuplicateCount(
            ruled.matches.filter((m) => m.matched && m.duplicate_of_match_id).length,
          );
          if (fresh.status === "failed") {
            setError(fresh.error_message);
            setPhase("error");
          } else {
            setPhase("results");
          }
        }
      } catch (e) {
        if (!cancelled) {
          setPhase("error");
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [resumeId]);

  // ---- actions ------------------------------------------------------------

  const digits = value.replace(/\D/g, "");
  const expected = searchType === "npi" ? 10 : 9;
  const valid = digits.length === expected;

  async function startSearch() {
    setError(null);
    setMatchData(null);
    setRuledOut([]);
    setDuplicateCount(0);
    setSelected(new Set());
    setRows({});
    setQueuedJobs([]);
    setBulkError(null);
    setStopping(false);
    setPage(0);
    setPhase("searching");
    try {
      const s = await api.createSearch({
        insurance_company: company,
        search_type: searchType,
        search_value: digits,
        hint: hint.trim() || undefined,
        hint_strict: hint.trim() ? hintStrict : undefined,
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
      // a queued search is cancelled instantly; a running one flips when the
      // worker sees the flag - polling picks that up and calls finalize()
      if (s.status === "cancelled") finalize();
    } catch (e) {
      // 409 = it already finished on its own; just show what it found
      if (e instanceof ApiError && e.status === 409) finalize();
      else {
        setStopping(false);
        setBulkError(e instanceof Error ? e.message : String(e));
      }
    }
  }

  function reset() {
    setPhase("idle");
    setSearch(null);
    setMatchData(null);
    setRuledOut([]);
    setDuplicateCount(0);
    setError(null);
    setSelected(new Set());
    setRows({});
    setQueuedJobs([]);
    setBulkError(null);
    setStopping(false);
    setPage(0);
  }

  function goToPage(p: number) {
    setPage(p);
    pageRef.current = p;
    refreshMatches(p);
  }

  function codeBody() {
    // The set id rides along so the job (and its download filename) can
    // carry the set's name; the explicit codes are what actually run.
    return {
      ...(codeSel.codes.length ? { codes: codeSel.codes } : {}),
      ...(codeSel.codeSetId ? { code_set_id: codeSel.codeSetId } : {}),
    };
  }

  // Every extraction goes through the scope dialog first: "just this
  // provider" vs "everyone billing these codes" is a 1000x difference in
  // output, so it is asked, not assumed. The answer is remembered as the
  // default for the next one (and mirrored in Extraction settings).
  type Pending =
    | { kind: "one"; match: SearchMatch }
    | { kind: "many"; body: { match_ids?: string[]; top_n?: number }; count: number };
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingBusy, setPendingBusy] = useState(false);

  function extractOne(m: SearchMatch) {
    setPending({ kind: "one", match: m });
  }

  function extractMany(body: { match_ids?: string[]; top_n?: number }, count: number) {
    setPending({ kind: "many", body, count });
  }

  async function confirmExtract(scope: ExtractScope) {
    if (!pending || !searchId) return;
    const filter = scope === "target";
    setFilterToTarget(filter);
    setPendingBusy(true);
    try {
      if (pending.kind === "one") {
        await runExtractOne(pending.match, filter);
      } else {
        await runExtractMany(pending.body, filter);
      }
      setPending(null);
    } finally {
      setPendingBusy(false);
    }
  }

  async function runExtractOne(m: SearchMatch, filter: boolean) {
    if (!searchId) return;
    setRows((r) => ({ ...r, [m.id]: { busy: true, error: null } }));
    try {
      const out = await api.extractSelection(searchId, {
        match_ids: [m.id],
        filter_to_target: filter,
        ...codeBody(),
      });
      setQueuedJobs((q) => [...out.jobs.filter((j) => !q.some((x) => x.id === j.id)), ...q]);
      setRows((r) => ({ ...r, [m.id]: { busy: false, error: null } }));
      refreshMatches(pageRef.current);
    } catch (e) {
      setRows((r) => ({
        ...r,
        [m.id]: { busy: false, error: e instanceof Error ? e.message : String(e) },
      }));
    }
  }

  async function runExtractMany(body: { match_ids?: string[]; top_n?: number }, filter: boolean) {
    if (!searchId) return;
    setBulkBusy(true);
    setBulkError(null);
    try {
      const out = await api.extractSelection(searchId, {
        ...body,
        filter_to_target: filter,
        ...codeBody(),
      });
      setQueuedJobs((q) => [...out.jobs.filter((j) => !q.some((x) => x.id === j.id)), ...q]);
      setSelected(new Set());
      refreshMatches(pageRef.current);
    } catch (e) {
      setBulkError(e instanceof Error ? e.message : String(e));
    } finally {
      setBulkBusy(false);
    }
  }

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function stopCptPolling(matchId: string) {
    const t = cptPollTimers.current[matchId];
    if (t) {
      window.clearInterval(t);
      delete cptPollTimers.current[matchId];
    }
  }

  function pollCptOnce(sid: string, m: SearchMatch) {
    api
      .pollMatchCpt(sid, m.id)
      .then((snap) => {
        setCptChecks((prev) => ({ ...prev, [m.id]: snap }));
        if (snap.status !== "running") stopCptPolling(m.id);
      })
      .catch(() => {
        // a transient poll failure isn't fatal - the next tick tries again.
        // Only give up (and stop the button spinning forever) if the timer
        // itself is gone, which stopCpt/unmount already handle.
      });
  }

  async function checkCpt(m: SearchMatch) {
    if (!searchId) return;
    if (!codeSel.codes.length && !codeSel.codeSetId) return;
    setCptStartErrors((prev) => {
      const { [m.id]: _drop, ...rest } = prev;
      return rest;
    });
    stopCptPolling(m.id);
    try {
      const snap = await api.startMatchCpt(searchId, m.id, {
        ...(codeSel.codes.length ? { codes: codeSel.codes } : {}),
        ...(codeSel.codeSetId ? { code_set_id: codeSel.codeSetId } : {}),
      });
      setCptChecks((prev) => ({ ...prev, [m.id]: snap }));
      if (snap.status === "running") {
        cptPollTimers.current[m.id] = window.setInterval(() => pollCptOnce(searchId, m), 1000);
      }
    } catch (e) {
      setCptStartErrors((prev) => ({
        ...prev,
        [m.id]: e instanceof Error ? e.message : String(e),
      }));
    }
  }

  async function stopCpt(m: SearchMatch) {
    if (!searchId) return;
    try {
      const snap = await api.cancelMatchCpt(searchId, m.id);
      setCptChecks((prev) => ({ ...prev, [m.id]: snap }));
    } finally {
      stopCptPolling(m.id);
    }
  }

  // ---- derived ------------------------------------------------------------

  const matches = matchData?.matches ?? [];
  const matchedCount = matchData?.matched_count ?? live?.files_matched ?? search?.files_matched ?? 0;
  const scannedCount = matchData?.scanned_count ?? live?.files_scanned ?? search?.files_scanned ?? 0;
  const total = matchData?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const searching = phase === "searching";
  const stoppedEarly = search?.status === "cancelled";
  const searchingLabel = searchType.toUpperCase();
  const targetLabel = search ? `${search.search_type.toUpperCase()} ${search.search_value}` : null;
  const selectedOnPage = matches.filter((m) => selected.has(m.id) && !m.job_id);
  const anyRowBusy = Object.values(rows).some((r) => r.busy);

  return (
    <PageTransition>
      <PageHeader
        title="Find a provider's rate files"
        subtitle="Pick a payer, search its published MRFs by NPI or TIN, then extract only the files that actually contain that provider."
      />

      {/* ================= Search form ================= */}
      <GlassCard className="p-6">
        <label className="mb-2 block text-[13px] font-medium text-dim">
          Insurance company
        </label>
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
              <span className="block text-[11px] uppercase tracking-wide text-faint">
                {c.key}
              </span>
              <span className="font-medium">{c.label.replace(/\s*\(.*\)/, "")}</span>
            </button>
          ))}
        </div>

        {/* Pre-flight: is this payer's index reachable from the server? Better
            to learn "blocked" here than after clicking Search. */}
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
                      layoutId="search-type-pill"
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
              How many of the payer&rsquo;s in-network files get opened and scanned for this
              ID. The index lists thousands; the likeliest are scanned first. Stop early
              once you have what you need.
            </p>
          </div>
        </div>

        <div className="mt-4">
          <label className="mb-2 block text-[13px] font-medium text-dim">
            Narrow by plan or region <span className="text-faint">(optional)</span>
          </label>
          <input
            className="input"
            placeholder={
              hintStrict
                ? "e.g. HMO — only files matching this are scanned, everything else skipped"
                : "e.g. Oxford, New York, Choice Plus — only changes which files are scanned first"
            }
            value={hint}
            disabled={searching}
            onChange={(e) => setHint(e.target.value)}
          />
          <label className="mt-2 flex items-center gap-2 text-xs text-dim">
            <input
              type="checkbox"
              checked={hintStrict}
              disabled={searching || !hint.trim()}
              onChange={(e) => setHintStrict(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-white/20 bg-white/5 accent-accent-500"
            />
            Only scan matching files
            <span className="text-faint">
              — instead of just prioritizing them, skip everything that doesn&rsquo;t
              match{hint.trim() ? "" : " (type something above first)"}
            </span>
          </label>
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
              New search
            </button>
          )}
          <span className="text-xs text-faint">
            {searching
              ? "Matches show up below as they're found — you can extract them and stop the scan any time."
              : `Scans up to ${formatNumber(maxFiles)} files${
                  meta?.search_concurrency ? `, ${meta.search_concurrency} at a time` : ""
                }.`}
          </span>
        </div>
      </GlassCard>

      {/* ================= Live progress =================
          Directly under the form, on purpose: this is what the user watches
          after clicking Search. Nothing tall may sit between the two. */}
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

      {/* ================= Error ================= */}
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

      {/* ================= Extraction settings =================
          One summary line by default. The panel stays mounted while hidden so
          typed codes survive collapsing (CodeSetPanel owns that state). */}
      <GlassCard className="mt-6 p-5" delay={0.05}>
        <button
          type="button"
          onClick={() => setSettingsOpen((o) => !o)}
          className="flex w-full flex-wrap items-center justify-between gap-3 text-left"
        >
          <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-sm font-semibold tracking-tightish">Extraction settings</span>
            <span className="text-xs text-dim tnum">
              {codeSel.codes.length
                ? `${codeSel.codes.length} custom codes`
                : "default code set (78 codes)"}
              {" · "}
              {filterToTarget
                ? `only rows for ${targetLabel ?? `the searched ${searchingLabel}`}`
                : "every provider group"}
            </span>
          </span>
          <span className="text-xs text-accent-400">{settingsOpen ? "Hide" : "Edit"}</span>
        </button>
        <div className={settingsOpen ? "mt-4" : "hidden"}>
          <CodeSetPanel onChange={setCodeSel} disabled={anyRowBusy || bulkBusy} />
          <label className="mt-5 flex cursor-pointer items-start gap-2.5">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-[#3ddc84]"
              checked={filterToTarget}
              onChange={(e) => setFilterToTarget(e.target.checked)}
            />
            <span className="text-xs">
              <span className="font-medium text-[var(--text)]">
                Only keep rows for {targetLabel ?? `the searched ${searchingLabel}`}
              </span>
              <span className="block text-faint">
                Unchecked, the CSVs hold every provider group in each file — far larger, and
                far slower to enrich. Every field the file carries is kept either way (extras
                land in the <code>extra_fields</code> column).
              </span>
            </span>
          </label>
          <p className="mt-3 text-[11px] text-faint">
            Applied to every &ldquo;Start extraction&rdquo; you click below.
          </p>
        </div>
      </GlassCard>

      {/* ================= Matches (live + final) ================= */}
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
              {matchedCount > 0 && (
                <div className="flex flex-wrap gap-2">
                  <button
                    className="btn btn-ghost !px-3 !py-1.5 !text-xs"
                    onClick={() =>
                      setSelected((prev) => {
                        const next = new Set(prev);
                        matches.filter((m) => !m.job_id).forEach((m) => next.add(m.id));
                        return next;
                      })
                    }
                  >
                    Select page
                  </button>
                  <button
                    className="btn btn-ghost !px-3 !py-1.5 !text-xs"
                    onClick={() => setSelected(new Set())}
                    disabled={selected.size === 0}
                  >
                    Clear
                  </button>
                </div>
              )}
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
            <>
              <GlassCard className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[1040px] text-sm">
                    <thead>
                      <tr className="border-b border-white/[0.06] text-left text-[11px] uppercase tracking-wide text-faint">
                        <th className="w-10 px-4 py-3" />
                        <th className="px-4 py-3 font-medium">File / plan</th>
                        <th className="px-4 py-3 font-medium">Evidence</th>
                        <th className="px-4 py-3 text-right font-medium">Size</th>
                        <th className="px-4 py-3 text-right font-medium">Est. time</th>
                        <th className="px-4 py-3 text-right font-medium">CPT matches</th>
                        <th className="px-4 py-3 pr-5 text-right font-medium">Extraction</th>
                      </tr>
                    </thead>
                    <tbody>
                      {matches.map((m) => (
                        <MatchRow
                          key={m.id}
                          match={m}
                          checked={selected.has(m.id)}
                          state={rows[m.id]}
                          onToggle={() => toggle(m.id)}
                          onExtract={() => extractOne(m)}
                          selectedCodes={codeSel.codes}
                          cptStatus={cptChecks[m.id]}
                          cptStartError={cptStartErrors[m.id]}
                          onCheckCpt={() => checkCpt(m)}
                          onStopCpt={() => stopCpt(m)}
                          targetLabel={targetLabel}
                        />
                      ))}
                      {matches.length === 0 && (
                        <tr>
                          <td colSpan={7} className="px-4 py-8 text-center text-xs text-faint">
                            Loading matches…
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                {pages > 1 && (
                  <div className="flex items-center justify-between border-t border-white/[0.06] px-4 py-3 text-xs text-faint">
                    <span className="tnum">
                      Page {page + 1} of {pages} · {formatNumber(total)} matching files
                    </span>
                    <div className="flex gap-2">
                      <button
                        className="btn btn-ghost !px-3 !py-1.5"
                        disabled={page === 0}
                        onClick={() => goToPage(Math.max(0, page - 1))}
                      >
                        Prev
                      </button>
                      <button
                        className="btn btn-ghost !px-3 !py-1.5"
                        disabled={page + 1 >= pages}
                        onClick={() => goToPage(page + 1)}
                      >
                        Next
                      </button>
                    </div>
                  </div>
                )}
              </GlassCard>

              {/* bulk action bar */}
              <GlassCard className="p-5">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="text-xs text-dim">
                    <span className="tnum">
                      {formatNumber(selected.size)} selected
                      {selectedOnPage.length !== selected.size ? " (across pages)" : ""}
                    </span>
                    <span className="block text-[11px] text-faint">
                      {codeSel.codes.length
                        ? `${codeSel.codes.length} custom codes`
                        : "default code set"}{" "}
                      · {filterToTarget ? "narrowed to this provider" : "every provider group"}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      className="btn btn-ghost"
                      disabled={bulkBusy || matchedCount === 0}
                      onClick={() => extractMany({ top_n: matchedCount }, matchedCount)}
                      title="Queue every matching file"
                    >
                      Extract all {formatNumber(matchedCount)}
                    </button>
                    <button
                      className="btn btn-primary"
                      disabled={bulkBusy || selected.size === 0}
                      onClick={() =>
                        extractMany({ match_ids: Array.from(selected) }, selected.size)
                      }
                    >
                      {bulkBusy ? "Queueing…" : `Extract selected (${selected.size})`}
                    </button>
                  </div>
                </div>
                {bulkError && <p className="mt-3 text-xs text-red-300">{bulkError}</p>}
                <p className="mt-3 text-[11px] text-faint">
                  Each file is downloaded, extracted for the chosen codes, then enriched against
                  NPPES automatically. Already-queued files are reused, not re-run.
                </p>
              </GlassCard>
            </>
          )}

          {queuedJobs.length > 0 && (
            <GlassCard className="p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-sm font-semibold tracking-tightish">
                  Queued from this search ({queuedJobs.length})
                </h3>
                <Link to={`/jobs?search=${search.id}`} className="text-xs text-accent-400 hover:underline">
                  Open all in Jobs →
                </Link>
              </div>
              <ul className="mt-3 divide-y divide-white/[0.05]">
                {queuedJobs.map((j) => (
                  <li key={j.id} className="flex items-center justify-between gap-3 py-2 text-xs">
                    <span className="min-w-0 flex-1 truncate text-dim" title={j.source_filename}>
                      {j.source_filename}
                    </span>
                    <span className="shrink-0 text-faint tnum">
                      {j.target_codes ? `${j.target_codes.length} codes` : "default codes"}
                    </span>
                    <StatusBadge status={j.status} size="xs" />
                    <Link to={`/jobs/${j.id}`} className="shrink-0 text-accent-400 hover:underline">
                      view →
                    </Link>
                  </li>
                ))}
              </ul>
            </GlassCard>
          )}

          {phase === "results" && ruledOut.length > 0 && <RuledOut matches={ruledOut} />}
        </motion.div>
      )}

      <ExtractScopeDialog
        open={pending !== null}
        fileCount={pending?.kind === "many" ? pending.count : 1}
        targetLabel={targetLabel ?? `the searched ${searchingLabel}`}
        codesSummary={
          codeSel.codes.length ? `${codeSel.codes.length} custom codes` : "default code set (78 codes)"
        }
        initialScope={filterToTarget ? "target" : "all"}
        busy={pendingBusy}
        onConfirm={confirmExtract}
        onCancel={() => {
          if (!pendingBusy) setPending(null);
        }}
      />
    </PageTransition>
  );
}

/* ---------------- pieces ---------------- */

function MatchRow({
  match,
  checked,
  state,
  onToggle,
  onExtract,
  selectedCodes,
  cptStatus,
  cptStartError,
  onCheckCpt,
  onStopCpt,
  targetLabel,
}: {
  match: SearchMatch;
  checked: boolean;
  state?: RowState;
  onToggle: () => void;
  onExtract: () => void;
  /** The CPT/HCPCS codes currently picked above the table. */
  selectedCodes: string[];
  /** A check started this session (running, done, cancelled or errored) -
   *  takes priority over the match's own persisted cpt_* fields, which may
   *  be from an earlier session. */
  cptStatus?: CptCheckStatus;
  /** Failed to even START the check (bad request, network drop) - distinct
   *  from the check itself erroring mid-scan, which lives on cptStatus. */
  cptStartError?: string;
  onCheckCpt: () => void;
  onStopCpt: () => void;
  /** "NPI 1234567893" / "TIN 123456789" - the search's own target, so the
   *  check's result can say who it's scoped to instead of just "matched". */
  targetLabel?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const queued = Boolean(match.job_id);
  const cptRunning = cptStatus?.status === "running";

  // Prefer a check made just now; otherwise fall back to whatever is
  // persisted on the match, but only if it was run for these exact codes -
  // a stale answer (checked before the user changed their selection) is
  // worse than no answer.
  const sameCodes = (a: string[] | null) =>
    Boolean(a) && a!.length === selectedCodes.length && a!.every((c) => selectedCodes.includes(c));

  const effective = cptStatus && (cptStatus.status === "done" || cptStatus.status === "cancelled")
    ? { matched: cptStatus.matched_codes, requested: cptStatus.requested_codes, exhausted: Boolean(cptStatus.exhausted), budgetExceeded: Boolean(cptStatus.budget_exceeded), stopped: cptStatus.status === "cancelled" }
    : sameCodes(match.cpt_checked_codes)
      ? {
          matched: match.cpt_matched_codes ?? [],
          requested: match.cpt_checked_codes ?? [],
          exhausted: Boolean(match.cpt_exhausted),
          budgetExceeded: Boolean(match.cpt_budget_exceeded),
          stopped: false,
        }
      : null;

  return (
    <>
      <tr
        className={`border-b border-white/[0.04] transition-colors ${
          checked ? "bg-accent-600/[0.07]" : "hover:bg-white/[0.03]"
        }`}
      >
        <td className="px-4 py-3.5">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[#3ddc84] disabled:opacity-30"
            checked={checked}
            disabled={queued}
            onChange={onToggle}
          />
        </td>
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
        <td className="px-4 py-3.5 text-right">
          {selectedCodes.length === 0 ? (
            <span className="text-[11px] text-faint">pick codes above</span>
          ) : effective ? (
            <span
              className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium tnum ${
                effective.matched.length > 0
                  ? "bg-accent-600/20 text-accent-400"
                  : "bg-white/[0.06] text-dim"
              }`}
              title={
                effective.matched.length
                  ? `Billed by ${targetLabel ?? "the searched provider"}: ${effective.matched.join(", ")}`
                  : `None of the selected codes are billed by ${targetLabel ?? "the searched provider"} in this file`
              }
            >
              {effective.matched.length}/{effective.requested.length}
              {effective.stopped && (
                <span className="text-amber-300" title="Stopped before the file was fully read - this count is a lower bound">
                  stopped
                </span>
              )}
              {!effective.stopped && effective.budgetExceeded && (
                <span className="text-amber-300" title="Scan budget reached before the whole file was read - this count is a lower bound">
                  *
                </span>
              )}
              {(effective.stopped || (!effective.exhausted && !effective.budgetExceeded)) && (
                <button
                  className="text-[10px] font-normal text-accent-400 hover:underline"
                  onClick={onCheckCpt}
                  title="Re-run the check"
                >
                  redo
                </button>
              )}
            </span>
          ) : cptRunning ? (
            <span className="inline-flex items-center gap-1.5">
              <span className="text-[11px] text-dim">
                {cptStatus?.phase === "download"
                  ? "Downloading…"
                  : cptStatus?.phase === "lookup"
                    ? "Resolving providers…"
                    : cptStatus?.phase === "scan"
                      ? "Checking codes…"
                      : "Checking…"}{" "}
                <Countdown
                  seconds={cptStatus?.eta_seconds}
                  active
                  suffix=""
                  className="text-[10px] text-faint"
                />
              </span>
              <button
                className="btn btn-ghost !px-2 !py-0.5 !text-[10px]"
                onClick={onStopCpt}
                title="Stop this check"
              >
                Stop
              </button>
            </span>
          ) : (
            <button
              className="btn btn-ghost !px-2.5 !py-1 !text-[11px]"
              onClick={onCheckCpt}
              title={`Checks whether ${targetLabel ?? "the searched provider"} itself bills the codes selected above in this file (no extraction) - downloads the file and resolves provider groups, so it takes roughly as long as a narrowed extraction's first two phases`}
            >
              Check codes
            </button>
          )}
          {cptStartError && (
            <span className="mt-1 block text-[10px] text-red-300" title={cptStartError}>
              {cptStartError.length > 50 ? cptStartError.slice(0, 50) + "…" : cptStartError}
            </span>
          )}
          {cptStatus?.status === "error" && cptStatus.error && (
            <span className="mt-1 block text-[10px] text-red-300" title={cptStatus.error}>
              {cptStatus.error.length > 50 ? cptStatus.error.slice(0, 50) + "…" : cptStatus.error}
            </span>
          )}
        </td>
        <td className="px-4 py-3.5 pr-5 text-right">
          {queued ? (
            <Link
              to={`/jobs/${match.job_id}`}
              className="inline-flex items-center gap-1 rounded-lg bg-accent-600/15 px-2.5 py-1.5 text-[11px] font-medium text-accent-400 ring-1 ring-accent-400/30 hover:bg-accent-600/25"
            >
              Queued · view →
            </Link>
          ) : (
            <button
              className="btn btn-primary !px-3 !py-1.5 !text-xs"
              disabled={state?.busy}
              onClick={onExtract}
            >
              {state?.busy ? "Queueing…" : "Start extraction"}
            </button>
          )}
          {state?.error && (
            <span className="mt-1 block text-[10px] text-red-300" title={state.error}>
              {state.error.length > 60 ? state.error.slice(0, 60) + "…" : state.error}
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr className="border-b border-white/[0.04] bg-black/20">
          <td />
          <td colSpan={6} className="px-4 py-3">
            <p className="mb-1 text-[10px] uppercase tracking-wide text-faint">Where it was found</p>
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all text-[10.5px] leading-relaxed text-dim">
              {match.context || "no context captured"}
            </pre>
            <p className="mt-2 break-all text-[10.5px] text-faint">{match.location}</p>
          </td>
        </tr>
      )}
    </>
  );
}

function RuledOut({ matches }: { matches: SearchMatch[] }) {
  const [open, setOpen] = useState(false);
  const inconclusive = matches.filter((m) => m.budget_exceeded || m.error);
  return (
    <GlassCard className="p-5">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between text-left">
        <span className="text-sm font-medium">
          {formatNumber(matches.length)} files ruled out
          {inconclusive.length > 0 && (
            <span className="ml-2 text-[11px] text-amber-300">
              ({formatNumber(inconclusive.length)} inconclusive)
            </span>
          )}
        </span>
        <span className="text-xs text-faint">{open ? "hide" : "show"}</span>
      </button>
      {open && (
        <ul className="mt-3 max-h-72 space-y-1.5 overflow-auto">
          {matches.map((m) => (
            <li
              key={m.id}
              className="flex items-baseline justify-between gap-3 border-b border-white/[0.04] pb-1.5 text-[11px]"
            >
              <span className="min-w-0 flex-1 truncate text-dim" title={m.filename}>
                {m.plan_name || m.filename}
              </span>
              <span className="shrink-0 tnum text-faint">{formatBytes(m.bytes_scanned)} read</span>
              <span className="w-28 shrink-0 text-right">
                {m.error ? (
                  <span className="text-red-300" title={m.error}>unreachable</span>
                ) : m.budget_exceeded ? (
                  <span className="text-amber-300">scan capped</span>
                ) : (
                  <span className="text-faint">not present</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {inconclusive.length > 0 && (
        <p className="mt-3 text-[11px] leading-relaxed text-faint">
          &ldquo;Scan capped&rdquo; means the file was larger than the per-file byte budget and was
          only partly read — the provider could still be in the part that wasn&rsquo;t reached. Raise{" "}
          <code>SEARCH_PROBE_MAX_BYTES</code> to rule those in or out.
        </p>
      )}
    </GlassCard>
  );
}

function Stat({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-faint">{label}</p>
      <p className={`mt-0.5 text-lg font-semibold tracking-tightish tnum ${accent ? "text-accent-400" : ""}`}>
        {value}
      </p>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4">
      <circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth="1.7" />
      <path d="M16 16l4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
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
