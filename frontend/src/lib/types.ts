// "cancelled" = stopped on purpose by the user (searches only, today).
export type JobStatus = "queued" | "processing" | "completed" | "failed" | "cancelled";

export interface Company {
  key: string;
  label: string;
}

export interface Job {
  id: string;
  insurance_company: string;
  insurance_company_label: string;
  source_filename: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  total_bytes: number | null;
  bytes_processed: number;
  records_processed: number;
  current_stage: string | null;
  output_size_bytes: number | null;
  record_count: number | null;
  output_file_count: number | null;
  error_message: string | null;
  percent_complete: number;
  eta_seconds: number | null;
  duration_seconds: number | null;
  has_output: boolean;
  source_url: string | null;
  search_id: string | null;
  target_type: string | null;
  target_value: string | null;
  filter_to_target: boolean;
  downloaded_bytes: number;
  throughput_bps: number | null;
  /** null = the built-in default code set */
  target_codes: string[] | null;
  cancel_requested: boolean;
  plan_name: string | null;
  code_set_name: string | null;
  /** the single-workbook download exists (output fit in one Excel sheet) */
  has_xlsx: boolean;
  /** e.g. UHC_Oxford-Metro-27_NPI1013988054_20260914_default.xlsx */
  download_name: string | null;
  /** How many of the requested CPT/HCPCS codes were requested vs actually
   *  found in the file. Both null until the job completes. A 0 record_count
   *  with matched_code_count 0 means the codes never appeared in the file at
   *  all; with matched_code_count > 0 it means they appeared, just not
   *  billed by the searched provider. */
  requested_code_count: number | null;
  matched_code_count: number | null;
  matched_codes: string[] | null;
}

/** manifest.json written by the worker next to the CSV parts. */
export interface JobManifest {
  job_id: string;
  insurance_company: string;
  source_filename: string;
  source_url?: string | null;
  source_size_bytes?: number;
  row_count: number;
  csv_parts: string[];
  columns: string[];
  target: { type: string; value: string } | null;
  /** null = built-in default code set */
  target_codes: string[] | null;
  extract_stats: {
    items_scanned?: number;
    rows?: number;
    dropped_off_target?: number;
    dropped_npi_eq_tin?: number;
    matched_codes?: string[];
    codes_requested?: string[];
    custom_codes?: boolean;
    extra_columns?: string[];
  };
  enrich_stats: {
    rows_written?: number;
    rows_skipped_npi_eq_tin?: number;
    unique_npis?: number;
  };
}

export interface JobList {
  jobs: Job[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobStatusLite {
  id: string;
  status: JobStatus;
  percent: number;
  eta_seconds: number | null;
  stage: string | null;
  records_processed: number;
  bytes_processed: number;
  total_bytes: number | null;
  error_message: string | null;
  downloaded_bytes: number;
  throughput_bps: number | null;
  cancel_requested: boolean;
}

export interface KpiTotals {
  total_jobs: number;
  total_records: number;
  total_storage_bytes: number;
  success_rate: number;
  completed_jobs: number;
  failed_jobs: number;
  processing_jobs: number;
  queued_jobs: number;
}

export interface CompanyCount {
  company: string;
  company_label: string;
  jobs: number;
  records: number;
}

export interface VolumePoint {
  date: string;
  jobs: number;
  records: number;
}

export interface Kpis {
  totals: KpiTotals;
  by_company: CompanyCount[];
  volume_over_time: VolumePoint[];
  recent_jobs: Job[];
  queue_eta_seconds: number | null;
}

/** One row of `kpi_snapshots` - what the KPIs looked like at a point in
 *  time. The live `Kpis` above are always recomputed; these are the history
 *  behind them, for trending. */
export interface KpiSnapshot {
  id: string;
  captured_at: string;
  source: "api" | "worker" | "manual";
  totals: KpiTotals;
  queue_eta_seconds: number | null;
  total_searches: number;
  completed_searches: number;
  total_matches: number;
  by_company: CompanyCount[];
  volume_over_time: VolumePoint[];
}

export interface Meta {
  extraction_mode: "stub" | "real";
  upload_chunk_size: number;
  max_upload_bytes: number;
  csv_row_threshold: number;
  companies: Company[];
  search_max_files: number;
  search_concurrency: number;
  search_probe_max_bytes: number;
}

// --- Provider (NPI/TIN) search --------------------------------------------

export type SearchType = "npi" | "tin";

export interface PayerSource {
  key: string;
  label: string;
  publisher_url: string;
  verified: string;
  notes: string;
  index_url: string | null;
  size_bytes?: number | null;
  resolved?: boolean;
  from_cache?: boolean;
  /** hand-pinned via the UI / payer_sources.json */
  pinned?: boolean;
  /** the host refused the TLS handshake - a firewall, not a wrong URL */
  blocked?: boolean;
  blocked_host?: string | null;
  error?: string | null;
  tried?: { url: string; ok: boolean; error: string | null; size_bytes: number | null }[];
}

export interface ColumnProbeResult {
  company: string;
  sampled_file: string;
  sampled_filename: string;
  index_source: string;
  file_size_bytes: number | null;
  items_sampled: number;
  exhausted: boolean;
  budget_exceeded: boolean;
  elapsed_seconds: number;
  item_level_columns: string[];
  item_level_standard_columns: string[];
  item_level_extra_columns: string[];
  price_level_columns: string[];
  price_level_standard_columns: string[];
  price_level_extra_columns: string[];
  error: string | null;
}

export interface ProviderSearch {
  id: string;
  insurance_company: string;
  insurance_company_label: string;
  search_type: SearchType;
  search_value: string;
  hint: string | null;
  hint_strict: boolean;
  status: JobStatus;
  current_stage: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  index_url: string | null;
  files_listed: number;
  files_to_scan: number;
  files_scanned: number;
  files_matched: number;
  bytes_scanned: number;
  percent_complete: number;
  eta_seconds: number | null;
  duration_seconds: number | null;
  error_message: string | null;
  cancel_requested: boolean;
  /** Codes chosen before this search ran - required, and what gated every
   *  match: a file only appears if its target bills at least one of these. */
  target_codes: string[];
  code_set_name: string | null;
}

export interface SearchStatusLite {
  id: string;
  status: JobStatus;
  percent: number;
  eta_seconds: number | null;
  stage: string | null;
  files_listed: number;
  files_to_scan: number;
  files_scanned: number;
  files_matched: number;
  bytes_scanned: number;
  error_message: string | null;
  cancel_requested: boolean;
}

export interface SearchMatch {
  id: string;
  search_id: string;
  location: string;
  filename: string;
  description: string | null;
  plan_name: string | null;
  plan_id: string | null;
  plan_market_type: string | null;
  reference_count: number;
  file_size_bytes: number | null;
  matched: boolean;
  confidence: string | null;
  provider_group_id: string | null;
  matched_tin: string | null;
  npi_count: number;
  context: string | null;
  bytes_scanned: number;
  scan_seconds: number | null;
  exhausted: boolean;
  budget_exceeded: boolean;
  error: string | null;
  job_id: string | null;
  /** Set when this is the same network as an earlier match in this search,
   *  republished under a second legal entity (same TIN, near-identical
   *  size) - excluded from matched_only=true results by default. Points at
   *  the earlier, canonical match's id. */
  duplicate_of_match_id: string | null;
  estimated_seconds: number | null;
  /** Which of the search's own target_codes the target TIN/NPI was
   *  confirmed to bill here (provider-group resolved at search time) - why
   *  this file is in the results at all. Always populated for a match. */
  target_billed_codes: string[] | null;
  /** When target_billed_codes was resolved - either automatically at search
   *  time (a Search-page search, which always has codes), or on demand via
   *  "Discover billed codes" (a codeless Provider Lookup search). Null means
   *  neither has happened yet - not the same as target_billed_codes being an
   *  empty list, which means it ran and found nothing. */
  target_codes_discovered_at: string | null;
  target_codes_discovery_error: string | null;
  /** Cached result of the last CPT/HCPCS coverage check (POST
   *  .../cpt-check), independent of the NPI/TIN evidence above. Null until
   *  someone runs it. cpt_checked_codes says which codes produced
   *  cpt_matched_codes, so a stale result (codes changed since) is
   *  detectable client-side. */
  cpt_checked_at: string | null;
  cpt_checked_codes: string[] | null;
  cpt_matched_codes: string[] | null;
  cpt_items_scanned: number | null;
  cpt_exhausted: boolean | null;
  cpt_budget_exceeded: boolean | null;
  cpt_error: string | null;
}

/** POST/GET/DELETE /api/searches/{id}/matches/{id}/cpt-check response - a
 *  running or finished background check, polled for live progress. */
export interface CptCheckStatus {
  match_id: string;
  status: "running" | "done" | "error" | "cancelled";
  /** "download" | "lookup" | "scan" while running - which pass the check is
   *  in. "lookup" resolves provider groups (the pass that makes this a
   *  target-scoped answer instead of "appears anywhere in the file"). */
  phase?: string | null;
  requested_codes: string[];
  matched_codes: string[];
  items_scanned: number;
  bytes_scanned: number;
  file_size_bytes: number | null;
  elapsed_seconds: number;
  eta_seconds: number | null;
  exhausted: boolean | null;
  budget_exceeded: boolean | null;
  error: string | null;
}

/** POST/GET/DELETE /api/searches/{id}/matches/{id}/discover-codes response -
 *  the Provider Lookup page's "what does this provider bill here, at all"
 *  full discovery run. No requested_codes: there's no fixed list, this is
 *  everything the target bills, found so far. */
export interface DiscoverCodesStatus {
  match_id: string;
  status: "running" | "done" | "error" | "cancelled";
  /** "download" | "lookup" | "scan" while running. */
  phase?: string | null;
  discovered_codes: string[];
  items_scanned: number;
  file_size_bytes: number | null;
  elapsed_seconds: number;
  eta_seconds: number | null;
  exhausted: boolean | null;
  budget_exceeded: boolean | null;
  error: string | null;
}

export interface MatchList {
  search: ProviderSearch;
  /** One page - see total/limit/offset. The counts below are whole-search. */
  matches: SearchMatch[];
  total: number;
  limit: number;
  offset: number;
  matched_count: number;
  scanned_count: number;
  estimated_total_seconds: number | null;
}

// --- Saved CPT/HCPCS code sets ---------------------------------------------

export interface CodeSet {
  id: string;
  name: string;
  codes: string[];
  code_count: number;
  created_at: string;
  updated_at: string;
}

export interface ExtractSelectionResult {
  search_id: string;
  created: number;
  jobs: Job[];
  estimated_total_seconds: number | null;
}
