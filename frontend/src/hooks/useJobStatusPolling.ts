import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { JobStatusLite } from "../lib/types";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

/**
 * Polls GET /api/jobs/{id}/status every `intervalMs` while the job is
 * queued/processing, and stops the moment it reaches completed/failed.
 * `onSettled` fires once on that transition (used to refetch full detail).
 */
export function useJobStatusPolling(
  jobId: string,
  initialStatus: JobStatusLite["status"] | undefined,
  onSettled: () => void,
  intervalMs = 3500,
) {
  const [status, setStatus] = useState<JobStatusLite | null>(null);
  const settledFired = useRef(false);
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    settledFired.current = false;
    if (initialStatus && TERMINAL.has(initialStatus)) return;

    let stopped = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const s = await api.getJobStatus(jobId);
        if (stopped) return;
        setStatus(s);
        if (TERMINAL.has(s.status)) {
          if (!settledFired.current) {
            settledFired.current = true;
            onSettledRef.current();
          }
          return; // stop polling
        }
      } catch {
        /* transient — keep polling */
      }
      if (!stopped) timer = window.setTimeout(poll, intervalMs);
    };

    timer = window.setTimeout(poll, intervalMs);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId, initialStatus, intervalMs]);

  return status;
}
