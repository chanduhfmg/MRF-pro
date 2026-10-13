import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { formatBytes } from "../lib/format";
import type { PayerSource } from "../lib/types";

type Kind = "loading" | "live" | "blocked" | "missing" | "error";

function kindOf(p: PayerSource | null, loading: boolean, failed: boolean): Kind {
  if (loading) return "loading";
  if (failed || !p) return "error";
  if (p.resolved) return "live";
  if (p.blocked) return "blocked";
  return "missing";
}

/**
 * Where a payer's table of contents is and whether this server can reach it.
 *
 * Compact form sits under the payer selector so a blocked or moved index is
 * known *before* the search button is clicked. The full form appears in the
 * search-failed card, with the "Pin index URL" control the error text points
 * at, and a retry.
 */
export default function PayerIndexPanel({
  payerKey,
  compact = false,
  onRetry,
}: {
  payerKey: string;
  compact?: boolean;
  onRetry?: () => void;
}) {
  const [payer, setPayer] = useState<PayerSource | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(!compact);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = useCallback(
    async (force = false) => {
      setLoading(true);
      setFailed(false);
      try {
        setPayer(await api.getPayer(payerKey, true, force));
      } catch {
        setFailed(true);
      } finally {
        setLoading(false);
      }
    },
    [payerKey],
  );

  useEffect(() => {
    setOpen(!compact);
    setMsg(null);
    load();
  }, [load, compact]);

  async function pin() {
    const u = url.trim();
    if (!u) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.pinPayerIndex(payerKey, u);
      setPayer(r);
      setUrl("");
      setMsg({ kind: "ok", text: "Pinned and verified - this URL serves a JSON index." });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  async function unpin() {
    setBusy(true);
    setMsg(null);
    try {
      setPayer(await api.unpinPayerIndex(payerKey));
      setMsg({ kind: "ok", text: "Pin removed; back to the built-in resolver." });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  const kind = kindOf(payer, loading, failed);
  const tone =
    kind === "live"
      ? "text-accent-400"
      : kind === "blocked"
        ? "text-red-300"
        : kind === "missing"
          ? "text-amber-300"
          : "text-faint";
  const dot =
    kind === "live"
      ? "bg-accent-400"
      : kind === "blocked"
        ? "bg-red-400"
        : kind === "missing"
          ? "bg-amber-400"
          : "bg-zinc-500";

  const headline =
    kind === "loading"
      ? "Checking where this payer publishes its files…"
      : kind === "error"
        ? "Couldn't check this payer's index right now."
        : kind === "live"
          ? `Index reachable${payer?.size_bytes ? ` · ${formatBytes(payer.size_bytes)}` : ""}${
              payer?.pinned ? " · pinned" : ""
            }`
          : kind === "blocked"
            ? `Index blocked - ${payer?.blocked_host} refuses automated connections`
            : "Can't find the payer MRF file - pin its URL below";

  return (
    <div
      className={`rounded-xl border px-3.5 py-2.5 text-xs ${
        kind === "blocked"
          ? "border-red-500/25 bg-red-500/[0.06]"
          : kind === "missing"
            ? "border-amber-500/25 bg-amber-500/[0.06]"
            : "border-white/10 bg-white/[0.02]"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={`inline-flex items-center gap-2 font-medium ${tone}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${dot} ${kind === "loading" ? "animate-pulse" : ""}`} />
          {headline}
        </span>
        <span className="flex items-center gap-3">
          {payer?.publisher_url && (
            <a
              href={payer.publisher_url}
              target="_blank"
              rel="noreferrer"
              className="text-faint hover:text-white hover:underline"
            >
              publisher page ↗
            </a>
          )}
          {compact && kind !== "loading" && (
            <button className="text-faint hover:text-white" onClick={() => setOpen((o) => !o)}>
              {open ? "hide" : "details"}
            </button>
          )}
        </span>
      </div>

      {open && kind !== "loading" && (
        <div className="mt-3 space-y-3">
          {kind === "blocked" && (
            <p className="leading-relaxed text-dim">
              {payer?.label} outsources publishing to <code>{payer?.blocked_host}</code>, whose
              firewall rejects the TLS handshake from automated clients - every client, from
              every network we tested. That is a policy on their side, not a wrong URL, so
              retrying won't help. If the publisher page opens in your browser and you can
              download the table of contents there, pin a URL this server <em>can</em> reach
              (an internal mirror, for instance). Otherwise this payer can't be searched from
              here.
            </p>
          )}
          {kind === "missing" && (
            <p className="leading-relaxed text-dim">
              We can't find this payer's MRF file. The payer may have moved or renamed its
              table of contents. Open the publisher page, copy the current index link, and pin
              it here. It's verified before it's saved.
            </p>
          )}
          {kind === "live" && payer?.index_url && (
            <p className="break-all text-[11px] text-faint">{payer.index_url}</p>
          )}
          {kind === "error" && (
            <p className="text-dim">
              The API didn't answer.{" "}
              <button className="text-accent-400 hover:underline" onClick={() => load(true)}>
                Try again
              </button>
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <input
              className="input !w-auto min-w-[260px] flex-1"
              placeholder="https://…/index.json.gz  — pin the index URL"
              value={url}
              disabled={busy}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") pin();
              }}
            />
            <button className="btn btn-ghost !py-2" disabled={busy || !url.trim()} onClick={pin}>
              {busy ? "Verifying…" : "Pin index URL"}
            </button>
            {payer?.pinned && (
              <button className="btn btn-ghost !py-2 text-red-300" disabled={busy} onClick={unpin}>
                Unpin
              </button>
            )}
            <button
              className="btn btn-ghost !py-2"
              disabled={busy}
              onClick={() => load(true)}
              title="Re-check the index right now, ignoring the cache"
            >
              Re-check
            </button>
            {onRetry && kind === "live" && (
              <button className="btn btn-primary !py-2" onClick={onRetry}>
                Retry search
              </button>
            )}
          </div>

          {msg && (
            <p className={msg.kind === "ok" ? "text-accent-400" : "text-red-300"}>{msg.text}</p>
          )}

          {payer?.tried && payer.tried.length > 0 && kind !== "live" && (
            <details className="text-[11px] text-faint">
              <summary className="cursor-pointer">What was tried ({payer.tried.length})</summary>
              <ul className="mt-1 space-y-0.5">
                {payer.tried.slice(0, 8).map((t) => (
                  <li key={t.url} className="break-all">
                    <span className={t.ok ? "text-accent-400" : "text-red-300"}>
                      {t.ok ? "ok" : t.error ?? "failed"}
                    </span>{" "}
                    · {t.url}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
