-- =====================================================================
-- Insurance MRF Extraction Dashboard - MySQL schema
-- Run once on the server:  mysql -u root -p < backend/schema.sql
-- (Local dev needs nothing: a SQLite file is auto-created when
--  DATABASE_URL is blank. The app also calls create_all() on startup,
--  so this file is mainly for provisioning MySQL by hand / in CI.)
-- =====================================================================

CREATE DATABASE IF NOT EXISTS mrf_pro
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE mrf_pro;

CREATE TABLE IF NOT EXISTS extraction_jobs (
  id                CHAR(36)      PRIMARY KEY,
  insurance_company VARCHAR(100)  NOT NULL,
  source_filename   VARCHAR(500)  NOT NULL,
  source_file_path  VARCHAR(1000) NOT NULL,
  status            ENUM('queued','processing','completed','failed','cancelled') NOT NULL DEFAULT 'queued',

  created_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- heartbeat: bumped on every progress write; used to detect dead jobs
  updated_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
  started_at        DATETIME      NULL,
  completed_at      DATETIME      NULL,

  total_bytes       BIGINT        NULL,
  bytes_processed   BIGINT        NOT NULL DEFAULT 0,
  records_processed BIGINT        NOT NULL DEFAULT 0,
  current_stage     VARCHAR(100)  NULL,

  output_zip_path   VARCHAR(1000) NULL,
  -- single-workbook .xlsx, written when the row count fits one sheet
  output_xlsx_path  VARCHAR(1000) NULL,
  output_size_bytes BIGINT        NULL,
  -- final frozen row count (records_processed is the live counter)
  record_count      BIGINT        NULL,
  output_file_count INT           NULL,

  -- how many of the requested CPT/HCPCS codes were actually found in the
  -- file (out of how many were requested), and which ones - a 0
  -- record_count is ambiguous without this: "codes never appeared" vs
  -- "codes appeared, just not billed by the searched provider"
  requested_code_count INT        NULL,
  matched_code_count   INT        NULL,
  matched_codes_json   TEXT       NULL,

  error_message     TEXT          NULL,

  -- worker bookkeeping (not in the original spec DDL; kept minimal)
  worker_id         VARCHAR(100)  NULL,
  retry_count       INT           NOT NULL DEFAULT 0,

  -- payer-sourced jobs: created by an NPI/TIN search instead of an upload.
  -- source_url set => the worker downloads the MRF before extracting.
  source_url        VARCHAR(2000) NULL,
  search_id         CHAR(36)      NULL,
  match_id          CHAR(36)      NULL,
  target_type       VARCHAR(10)   NULL,
  target_value      VARCHAR(40)   NULL,
  filter_to_target  TINYINT(1)    NOT NULL DEFAULT 0,
  downloaded_bytes  BIGINT        NOT NULL DEFAULT 0,
  -- JSON array of CPT/HCPCS codes; NULL = the script's built-in default set
  target_codes      TEXT          NULL,

  -- smoothed ETA written by the worker each heartbeat (see app/eta.py)
  eta_seconds_est   DOUBLE        NULL,
  throughput_bps    DOUBLE        NULL,

  INDEX ix_jobs_status      (status),
  INDEX ix_jobs_company     (insurance_company),
  INDEX ix_jobs_created_at  (created_at),
  INDEX ix_jobs_status_created (status, created_at),
  INDEX ix_jobs_search      (search_id)
) ENGINE=InnoDB;


-- =====================================================================
-- Provider searches: "which of this payer's in-network files contain
-- this NPI / TIN?" Queued and claimed exactly like extraction jobs.
-- =====================================================================
CREATE TABLE IF NOT EXISTS provider_searches (
  id                CHAR(36)      PRIMARY KEY,
  insurance_company VARCHAR(100)  NOT NULL,
  search_type       ENUM('npi','tin') NOT NULL,
  search_value      VARCHAR(40)   NOT NULL,
  -- digits only, leading zeros stripped: what the probe actually looks for
  search_value_norm VARCHAR(40)   NOT NULL,
  hint              VARCHAR(200)  NULL,
  -- FALSE (default): hint only reorders scanning. TRUE: only files whose
  -- plan name/description/filename contain `hint` are scanned at all.
  hint_strict       BOOLEAN       NOT NULL DEFAULT FALSE,
  -- codes chosen before this search ran; required. A file only becomes a
  -- match once the target is confirmed to bill one of these.
  target_codes_json TEXT          NULL,
  code_set_name     VARCHAR(200)  NULL,

  status            ENUM('queued','processing','completed','failed','cancelled') NOT NULL DEFAULT 'queued',
  current_stage     VARCHAR(100)  NULL,

  created_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
  started_at        DATETIME      NULL,
  completed_at      DATETIME      NULL,

  index_url         VARCHAR(2000) NULL,
  files_listed      INT           NOT NULL DEFAULT 0,
  files_to_scan     INT           NOT NULL DEFAULT 0,
  files_scanned     INT           NOT NULL DEFAULT 0,
  files_matched     INT           NOT NULL DEFAULT 0,
  bytes_scanned     BIGINT        NOT NULL DEFAULT 0,
  -- fractional progress incl. partly-read files; drives the percentage
  scan_fraction     DOUBLE        NULL,
  eta_seconds_est   DOUBLE        NULL,

  error_message     TEXT          NULL,
  -- set by "Stop process"; the worker polls it and winds the scan down
  cancel_requested  TINYINT(1)    NOT NULL DEFAULT 0,
  worker_id         VARCHAR(100)  NULL,
  retry_count       INT           NOT NULL DEFAULT 0,

  INDEX ix_search_status  (status),
  INDEX ix_search_company (insurance_company),
  INDEX ix_search_value   (search_value),
  INDEX ix_search_created (created_at)
) ENGINE=InnoDB;


-- =====================================================================
-- One row per in-network file examined by a search. Non-matching files
-- are kept too, so "3 hits in 40 files scanned" is answerable and a
-- re-run can skip what was already ruled out.
-- =====================================================================
CREATE TABLE IF NOT EXISTS search_matches (
  id                CHAR(36)      PRIMARY KEY,
  search_id         CHAR(36)      NOT NULL,

  location          VARCHAR(2000) NOT NULL,
  filename          VARCHAR(500)  NOT NULL,
  description       VARCHAR(1000) NULL,
  plan_name         VARCHAR(500)  NULL,
  plan_id           VARCHAR(100)  NULL,
  plan_market_type  VARCHAR(50)   NULL,
  reference_count   INT           NOT NULL DEFAULT 1,

  file_size_bytes   BIGINT        NULL,
  matched           TINYINT(1)    NOT NULL DEFAULT 0,
  -- 'strong' = found inside a provider group; 'weak' = digits only
  confidence        VARCHAR(20)   NULL,
  provider_group_id VARCHAR(100)  NULL,
  matched_tin       VARCHAR(40)   NULL,
  npi_count         INT           NOT NULL DEFAULT 0,
  context           TEXT          NULL,
  -- set when this match is the same network as an earlier match in this
  -- search, republished under a second legal entity (same TIN, near-
  -- identical size) - points at the earlier (canonical) match's id.
  -- matched_only=true hides these.
  duplicate_of_match_id CHAR(36)  NULL,
  -- which of the search's target_codes the target TIN/NPI was confirmed to
  -- bill here (provider-group resolved) - why this file matched at all. Also
  -- doubles as the Provider Lookup page's on-demand "Discover billed codes"
  -- result for a codeless search (target_codes_discovered_at NULL until run).
  target_billed_codes_json TEXT   NULL,
  target_codes_discovered_at   DATETIME NULL,
  target_codes_discovery_error TEXT     NULL,

  bytes_scanned     BIGINT        NOT NULL DEFAULT 0,
  scan_seconds      DOUBLE        NULL,
  exhausted         TINYINT(1)    NOT NULL DEFAULT 0,
  budget_exceeded   TINYINT(1)    NOT NULL DEFAULT 0,
  error             TEXT          NULL,

  job_id            CHAR(36)      NULL,
  created_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

  -- cached CPT/HCPCS coverage check (cpt_probe.scan_billing_codes) - a
  -- separate, on-demand "which of my chosen codes are in this file?" scan,
  -- distinct from the NPI/TIN probe above. NULL until someone runs it.
  cpt_checked_at        DATETIME NULL,
  cpt_checked_codes_json TEXT    NULL,
  cpt_matched_codes_json TEXT    NULL,
  cpt_items_scanned     INT      NULL,
  cpt_bytes_scanned     BIGINT   NULL,
  cpt_exhausted         TINYINT(1) NULL,
  cpt_budget_exceeded   TINYINT(1) NULL,
  cpt_error             TEXT     NULL,

  INDEX ix_match_search  (search_id),
  INDEX ix_match_matched (matched),
  INDEX ix_match_job     (job_id)
) ENGINE=InnoDB;

-- Suggested app user (adjust host / password for your setup):
--   CREATE USER 'mrf'@'%' IDENTIFIED BY 'change-me';
--   GRANT ALL PRIVILEGES ON mrf_pro.* TO 'mrf'@'%';
--   FLUSH PRIVILEGES;


-- =====================================================================
-- Named, reusable CPT/HCPCS code lists ("E&M codes", "Cardiology", ...).
-- Chosen from a dropdown when starting an extraction.
-- =====================================================================
CREATE TABLE IF NOT EXISTS cpt_code_sets (
  id          CHAR(36)     PRIMARY KEY,
  name        VARCHAR(120) NOT NULL,
  -- JSON array of strings, upper-cased and de-duplicated on write
  codes_json  TEXT         NOT NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP,
  INDEX ix_code_sets_name (name)
) ENGINE=InnoDB;


-- =====================================================================
-- Persisted KPI history.
--
-- The dashboard's KPI numbers are computed live off extraction_jobs, so
-- nothing here is ever read back as "the current value". What this table
-- holds is what those numbers looked like at a point in time - which is
-- not recoverable once jobs are pruned or a ratio like success_rate
-- shifts underneath you.
--
-- One row per capture, breakdowns denormalised into JSON text: a snapshot
-- is written and read whole and never joined against, so keeping it to a
-- single INSERT keeps it off the critical path of a dashboard read.
-- =====================================================================
CREATE TABLE IF NOT EXISTS kpi_snapshots (
  id                  CHAR(36)     PRIMARY KEY,
  captured_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- 'api' = dashboard read, 'worker' = a job/search finished, 'manual'
  source              VARCHAR(20)  NOT NULL DEFAULT 'api',

  -- totals (mirror of the /api/kpis "totals" object)
  total_jobs          INT          NOT NULL DEFAULT 0,
  total_records       BIGINT       NOT NULL DEFAULT 0,
  total_storage_bytes BIGINT       NOT NULL DEFAULT 0,
  success_rate        DOUBLE       NOT NULL DEFAULT 0,
  completed_jobs      INT          NOT NULL DEFAULT 0,
  failed_jobs         INT          NOT NULL DEFAULT 0,
  processing_jobs     INT          NOT NULL DEFAULT 0,
  queued_jobs         INT          NOT NULL DEFAULT 0,
  queue_eta_seconds   DOUBLE       NULL,

  -- provider-search side of the house
  total_searches      INT          NOT NULL DEFAULT 0,
  completed_searches  INT          NOT NULL DEFAULT 0,
  total_matches       INT          NOT NULL DEFAULT 0,

  -- [{company, company_label, jobs, records}, ...]
  by_company_json     TEXT         NULL,
  -- [{date, jobs, records}, ...] over the trailing window
  volume_json         TEXT         NULL,

  INDEX ix_kpi_captured (captured_at),
  INDEX ix_kpi_source   (source, captured_at)
) ENGINE=InnoDB;
