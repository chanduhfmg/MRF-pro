import type {
  Company,
  Job,
  JobList,
  JobStatus,
  JobStatusLite,
  Kpis,
  Meta,
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
  getJobStatus: (id: string) => req<JobStatusLite>(`/api/jobs/${id}/status`),

  downloadUrl: (id: string) => `${BASE}/api/jobs/${id}/download`,

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
};
