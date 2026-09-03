-- =====================================================================
-- Insurance MRF Extraction Dashboard - MySQL schema
-- Run once on the server:  mysql -u root -p < backend/schema.sql
-- (Local dev needs nothing: a SQLite file is auto-created when
--  DATABASE_URL is blank. The app also calls create_all() on startup,
--  so this file is mainly for provisioning MySQL by hand / in CI.)
-- =====================================================================

CREATE DATABASE IF NOT EXISTS mrf_dashboard
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE mrf_dashboard;

CREATE TABLE IF NOT EXISTS extraction_jobs (
  id                CHAR(36)      PRIMARY KEY,
  insurance_company VARCHAR(100)  NOT NULL,
  source_filename   VARCHAR(500)  NOT NULL,
  source_file_path  VARCHAR(1000) NOT NULL,
  status            ENUM('queued','processing','completed','failed') NOT NULL DEFAULT 'queued',

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
  output_size_bytes BIGINT        NULL,
  -- final frozen row count (records_processed is the live counter)
  record_count      BIGINT        NULL,
  output_file_count INT           NULL,

  error_message     TEXT          NULL,

  -- worker bookkeeping (not in the original spec DDL; kept minimal)
  worker_id         VARCHAR(100)  NULL,
  retry_count       INT           NOT NULL DEFAULT 0,

  INDEX ix_jobs_status      (status),
  INDEX ix_jobs_company     (insurance_company),
  INDEX ix_jobs_created_at  (created_at),
  INDEX ix_jobs_status_created (status, created_at)
) ENGINE=InnoDB;

-- Suggested app user (adjust host / password for your setup):
--   CREATE USER 'mrf'@'%' IDENTIFIED BY 'change-me';
--   GRANT ALL PRIVILEGES ON mrf_dashboard.* TO 'mrf'@'%';
--   FLUSH PRIVILEGES;
