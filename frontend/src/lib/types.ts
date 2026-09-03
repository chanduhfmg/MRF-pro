export type JobStatus = "queued" | "processing" | "completed" | "failed";

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
}

export interface Meta {
  extraction_mode: "stub" | "real";
  upload_chunk_size: number;
  max_upload_bytes: number;
  csv_row_threshold: number;
  companies: Company[];
}
