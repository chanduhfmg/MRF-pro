import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { SearchStatusLite } from "../lib/types";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

/**
 * Polls GET /api/searches/{id}/status while the search is queued/processing.
 *
 * Faster than the job poll (2s vs 3.5s) on purpose: someone is sitting on the
 * search screen watching the file counter move, so the extra request is worth
 * it. Stops the moment the search settles and fires `onSettled` once so the
 * page can load the matches.
 */
export function useSearchPolling(
  searchId: string | null,
  initialStatus: SearchStatusLite["status"] | undefined,
  onSettled: () => void,
  intervalMs = 2000,
) {
  const [status, setStatus] = useState<SearchStatusLite | null>(null);
  const settledFired = useRef(false);
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    setStatus(null);
    settledFired.current = false;
    if (!searchId) return;
    if (initialStatus && TERMINAL.has(initialStatus)) return;

    let stopped = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const s = await api.getSearchStatus(searchId);
        if (stopped) return;
        setStatus(s);
        if (TERMINAL.has(s.status)) {
          if (!settledFired.current) {
            settledFired.current = true;
            onSettledRef.current();
          }
          return;
        }
      } catch {
        /* transient - keep polling */
      }
      if (!stopped) timer = window.setTimeout(poll, intervalMs);
    };

    // poll immediately: the worker picks a search up within a second or two
    poll();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [searchId, initialStatus, intervalMs]);

  return status;
}
