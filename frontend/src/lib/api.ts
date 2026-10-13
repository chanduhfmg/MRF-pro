import type {
  CodeSet,
  ColumnProbeResult,
  Company,
  CptCheckStatus,
  DiscoverCodesStatus,
  ExtractSelectionResult,
  Job,
  JobList,
  JobManifest,
  JobStatus,
  JobStatusLite,
  Kpis,
  KpiSnapshot,
  MatchList,
  Meta,
  PayerSource,
  ProviderSearch,
  SearchStatusLite,
  SearchType,
} from "./types";

// Same-origin by default (dev proxy, or FastAPI serving the SPA, or a
// reverse proxy). Set VITE_API_BASE_URL when UI and API are split hosts.
const BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-json error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface JobFilters {
  company?: string;
  status?: JobStatus | "";
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export const api = {
  base: BASE,

  health: () => req<{ status: string; database: string; extraction_mode: string }>("/api/health"),
  meta: () => req<Meta>("/api/meta"),
  companies: () => req<Company[]>("/api/companies"),

  /** Samples one of the payer's current files and reports what fields it
   *  actually has - no extraction, just the schema. A few seconds, not a
   *  queued job. */
  payerColumns: (company: string, sampleItems = 150) =>
    req<ColumnProbeResult>(
      `/api/payers/${company}/columns?sample_items=${sampleItems}`,
    ),

  /** One CSV, every payer, one row per field - for "what columns does
   *  everyone have" instead of checking payer by payer. */
  allPayerColumnsDownloadUrl: (sampleItems = 100) =>
    `${BASE}/api/payers/columns/download?sample_items=${sampleItems}`,

  /** The "Convert File" tool - upload any JSON/JSON.GZ/CSV, pick an output
   *  format, get a file back. Not JSON in/out like `req`, so this does its
   *  own fetch and hands back the blob plus the filename the server chose. */
  convertFile: async (
    file: File,
    targetFormat: "csv" | "xlsx" | "json",
  ): Promise<{ blob: Blob; filename: string }> => {
    const body = new FormData();
    body.append("file", file);
    body.append("target_format", targetFormat);
    const res = await fetch(`${BASE}/api/convert`, { method: "POST", body });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const j = await res.json();
        if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* non-json error body */
      }
      throw new ApiError(res.status, detail);
    }
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const match = /filename="?([^"]+)"?/.exec(disposition);
    const filename = match?.[1] ?? `converted.${targetFormat}`;
    return { blob: await res.blob(), filename };
  },

  listJobs: (f: JobFilters = {}) => {
    const q = new URLSearchParams();
    if (f.company) q.set("company", f.company);
    if (f.status) q.set("status", f.status);
    if (f.date_from) q.set("date_from", f.date_from);
    if (f.date_to) q.set("date_to", f.date_to);
    q.set("limit", String(f.limit ?? 50));
    q.set("offset", String(f.offset ?? 0));
    return req<JobList>(`/api/jobs?${q.toString()}`);
  },

  getJob: (id: string) => req<Job>(`/api/jobs/${id}`),
  /** "Stop extraction" - queued jobs cancel instantly, running ones within seconds. */
  /** "Stop extraction" - the job is deleted, either immediately (was
   *  queued) or within a few seconds once the worker aborts (was
   *  processing). `deleted: true` means it's already gone. */
  cancelJob: (id: string) =>
    req<{ id: string; deleted: boolean; status: JobStatus | null; current_stage: string | null }>(
      `/api/jobs/${id}/cancel`,
      { method: "POST" },
    ),

  /** Retry a failed job - same file, same target, same codes, queued
   *  fresh. Only valid while the job is `failed`. */
  rerunJob: (id: string) => req<Job>(`/api/jobs/${id}/rerun`, { method: "POST" }),
  /** Extraction stats written beside the CSVs; 409 until the job has output. */
  getJobManifest: (id: string) => req<JobManifest>(`/api/jobs/${id}/manifest`),
  getJobStatus: (id: string) => req<JobStatusLite>(`/api/jobs/${id}/status`),

  /** xlsx = one workbook (default); zip = CSV parts + manifest. */
  downloadUrl: (id: string, format: "xlsx" | "zip" = "xlsx") =>
    `${BASE}/api/jobs/${id}/download?format=${format}`,

  // --- chunked upload ---
  initUpload: (body: { insurance_company: string; filename: string; file_size: number }) =>
    req<{ upload_id: string; chunk_size: number; received_chunks: number[] }>("/api/jobs/uploads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  uploadChunk: (uploadId: string, index: number, blob: Blob, signal?: AbortSignal) => {
    const fd = new FormData();
    fd.set("index", String(index));
    fd.set("chunk", blob, "chunk");
    return req<{ index: number; bytes: number }>(`/api/jobs/uploads/${uploadId}/chunks`, {
      method: "POST",
      body: fd,
      signal,
    });
  },

  completeUpload: (uploadId: string, totalChunks: number) =>
    req<Job>(`/api/jobs/uploads/${uploadId}/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ total_chunks: totalChunks }),
    }),

  abortUpload: (uploadId: string) =>
    req<{ ok: boolean }>(`/api/jobs/uploads/${uploadId}`, { method: "DELETE" }),

  kpis: () => req<Kpis>("/api/kpis"),

  /** Stored KPI snapshots, oldest first. */
  kpiHistory: (days = 30, limit = 500) =>
    req<KpiSnapshot[]>(`/api/kpis/history?days=${days}&limit=${limit}`),

  /** Force a KPI capture now, bypassing the server-side throttle. */
  snapshotKpis: () => req<KpiSnapshot>("/api/kpis/snapshot", { method: "POST" }),

  // --- payer MRF sources ---
  payers: (probe = false, force = false) => {
    const q = new URLSearchParams();
    if (probe) q.set("probe", "true");
    if (force) q.set("force", "true");
    const s = q.toString();
    return req<PayerSource[]>(`/api/payers${s ? `?${s}` : ""}`);
  },

  /** One payer's index status - checked before searching so a blocked or
   *  moved index is known up front. Successful resolutions are cached. */
  getPayer: (key: string, probe = true, force = false) =>
    req<PayerSource>(`/api/payers/${key}?probe=${probe}&force=${force}`),

  pinPayerIndex: (key: string, indexUrl: string) =>
    req<PayerSource>(`/api/payers/${key}/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index_url: indexUrl }),
    }),

  unpinPayerIndex: (key: string) =>
    req<PayerSource>(`/api/payers/${key}/index`, { method: "DELETE" }),

  // --- provider (NPI/TIN) search ---
  createSearch: (body: {
    insurance_company: string;
    search_type: SearchType;
    search_value: string;
    hint?: string;
    /** false (default): hint only reorders. true: only files whose plan
     *  name/description/filename contain `hint` are scanned at all. */
    hint_strict?: boolean;
    max_files?: number;
    // Required by the API: a file only becomes a match once the target
    // bills at least one of these.
    codes?: string[];
    code_set_id?: string;
  }) =>
    req<ProviderSearch>("/api/searches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  getSearch: (id: string) => req<ProviderSearch>(`/api/searches/${id}`),
  getSearchStatus: (id: string) => req<SearchStatusLite>(`/api/searches/${id}/status`),

  listSearches: (limit = 10, status?: JobStatus) =>
    req<{ searches: ProviderSearch[]; total: number }>(
      `/api/searches?limit=${limit}${status ? `&status=${status}` : ""}`,
    ),

  getMatches: (id: string, matchedOnly = true, limit = 10, offset = 0) =>
    req<MatchList>(
      `/api/searches/${id}/matches?matched_only=${matchedOnly}&limit=${limit}&offset=${offset}`,
    ),

  /** "Stop process" - ends the scan early, keeps every match found so far. */
  cancelSearch: (id: string) =>
    req<ProviderSearch>(`/api/searches/${id}/cancel`, { method: "POST" }),

  extractSelection: (
    id: string,
    body: {
      match_ids?: string[];
      top_n?: number;
      filter_to_target?: boolean;
      /** Explicit CPT/HCPCS codes; omit for the built-in default set. */
      codes?: string[];
      code_set_id?: string;
    },
  ) =>
    req<ExtractSelectionResult>(`/api/searches/${id}/extract`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  jobsForSearch: (id: string) => req<Job[]>(`/api/searches/${id}/jobs`),

  /** "Of these codes, how many actually appear in this one file?" - a cheap
   *  scan (no provider lookup, no row building), independent of extraction.
   *  Runs in the background server-side; poll `pollMatchCpt` for live
   *  progress/ETA and `cancelMatchCpt` to stop it. Cached on the match once
   *  done, so reopening the search re-serves it instead of re-scanning. */
  startMatchCpt: (
    searchId: string,
    matchId: string,
    body: { codes?: string[]; code_set_id?: string },
  ) =>
    req<CptCheckStatus>(`/api/searches/${searchId}/matches/${matchId}/cpt-check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  pollMatchCpt: (searchId: string, matchId: string) =>
    req<CptCheckStatus>(`/api/searches/${searchId}/matches/${matchId}/cpt-check`),

  cancelMatchCpt: (searchId: string, matchId: string) =>
    req<CptCheckStatus>(`/api/searches/${searchId}/matches/${matchId}/cpt-check`, {
      method: "DELETE",
    }),

  /** "What does this TIN/NPI bill in this file, at all?" - full discovery,
   *  no fixed code list (the Provider Lookup page's flow). Downloads the
   *  file and resolves provider groups, so it's slower than startMatchCpt -
   *  poll pollMatchDiscoverCodes for live progress/ETA, cancelMatchDiscoverCodes
   *  to stop it. Cached on the match once done. */
  startMatchDiscoverCodes: (searchId: string, matchId: string) =>
    req<DiscoverCodesStatus>(`/api/searches/${searchId}/matches/${matchId}/discover-codes`, {
      method: "POST",
    }),

  pollMatchDiscoverCodes: (searchId: string, matchId: string) =>
    req<DiscoverCodesStatus>(`/api/searches/${searchId}/matches/${matchId}/discover-codes`),

  cancelMatchDiscoverCodes: (searchId: string, matchId: string) =>
    req<DiscoverCodesStatus>(`/api/searches/${searchId}/matches/${matchId}/discover-codes`, {
      method: "DELETE",
    }),

  // --- saved CPT/HCPCS code sets ---
  listCodeSets: () => req<CodeSet[]>("/api/code-sets"),

  createCodeSet: (name: string, codes: string[]) =>
    req<CodeSet>("/api/code-sets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, codes }),
    }),

  updateCodeSet: (id: string, body: { name?: string; codes?: string[] }) =>
    req<CodeSet>(`/api/code-sets/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  deleteCodeSet: (id: string) =>
    req<{ ok: boolean }>(`/api/code-sets/${id}`, { method: "DELETE" }),
};
