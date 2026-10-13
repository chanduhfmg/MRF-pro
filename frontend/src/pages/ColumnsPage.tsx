import { useState } from "react";
import PageTransition from "../components/PageTransition";
import PageHeader from "../components/PageHeader";
import GlassCard from "../components/GlassCard";
import EmptyState from "../components/EmptyState";
import Select from "../components/Select";
import { api, ApiError } from "../lib/api";
import { formatBytes } from "../lib/format";
import type { ColumnProbeResult, Meta } from "../lib/types";

const FALLBACK_COMPANIES = [
  { key: "uhc", label: "UnitedHealthcare (UHC)" },
  { key: "aetna", label: "Aetna" },
  { key: "anthem", label: "Anthem" },
  { key: "cigna", label: "Cigna Healthcare" },
  { key: "mvp", label: "MVP Health Care" },
];

export default function ColumnsPage({ meta }: { meta: Meta | null }) {
  const companies = meta?.companies?.length ? meta.companies : FALLBACK_COMPANIES;
  const [company, setCompany] = useState(companies[0]?.key ?? "uhc");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ColumnProbeResult | null>(null);

  async function check() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.payerColumns(company);
      setResult(r);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const companyLabel = companies.find((c) => c.key === company)?.label ?? company;

  return (
    <PageTransition>
      <PageHeader
        title="Check Columns"
        subtitle="See what fields a payer's file actually contains, before running a real extraction."
        actions={
          <a
            className="btn btn-ghost"
            href={api.allPayerColumnsDownloadUrl()}
            title="One CSV, every payer, one row per field"
          >
            <DownloadIcon /> Download report (all payers)
          </a>
        }
      />

      <GlassCard className="mb-5 p-5">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[240px]">
            <label className="mb-1.5 block text-xs font-medium text-dim">Payer</label>
            <Select
              value={company}
              onChange={(v) => setCompany(v)}
              options={companies.map((c) => ({ value: c.key, label: c.label }))}
            />
          </div>
          <button className="btn btn-primary" disabled={loading} onClick={check}>
            {loading ? "Checking…" : "Check columns"}
          </button>
        </div>
        <p className="mt-2.5 text-[11px] leading-relaxed text-faint">
          Samples the first ~150 rate entries of one of {companyLabel}&rsquo;s current
          in-network files - a few seconds, not a full extraction - and reports every
          distinct field name found, split into the standard Transparency-in-Coverage
          columns and anything this payer adds beyond that.
        </p>
      </GlassCard>

      {loading && (
        <GlassCard className="p-8 text-center text-sm text-dim">
          Streaming a sample file…
        </GlassCard>
      )}

      {error && (
        <GlassCard className="p-6 text-sm text-red-300">{error}</GlassCard>
      )}

      {result && !loading && (
        <>
          <GlassCard className="mb-5 flex flex-wrap items-center justify-between gap-3 p-4">
            <div className="min-w-0">
              <div className="truncate text-sm font-medium" title={result.sampled_file}>
                {result.sampled_filename}
              </div>
              <div className="mt-0.5 text-[11px] text-faint">
                {result.index_source}
                {result.file_size_bytes != null && ` · ${formatBytes(result.file_size_bytes)} file`}
              </div>
            </div>
            <div className="text-right text-[11px] text-faint tnum">
              <div>
                sampled {result.items_sampled} entr{result.items_sampled === 1 ? "y" : "ies"}
                {result.exhausted ? " (whole file - that's all it has)" : ""}
                {result.budget_exceeded ? " (stopped at the sample budget)" : ""}
              </div>
              <div>{result.elapsed_seconds}s</div>
            </div>
          </GlassCard>

          {result.error ? (
            <EmptyState
              title="Couldn't read a sample file"
              hint={result.error}
            />
          ) : (
            <div className="grid gap-5 sm:grid-cols-2">
              <ColumnGroup
                title="Item-level fields"
                hint="One per billing code entry (name, code, description, …)."
                standard={result.item_level_standard_columns}
                extra={result.item_level_extra_columns}
              />
              <ColumnGroup
                title="Price-level fields"
                hint="One per negotiated price within a billing code."
                standard={result.price_level_standard_columns}
                extra={result.price_level_extra_columns}
              />
            </div>
          )}
        </>
      )}

      {!result && !loading && !error && (
        <EmptyState
          title="Pick a payer and check"
          hint="This reads field names only - no rates, no provider matching, nothing queued."
        />
      )}
    </PageTransition>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5">
      <path d="M12 4v10m0 0l-3.5-3.5M12 14l3.5-3.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 17v2a1 1 0 001 1h12a1 1 0 001-1v-2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function ColumnGroup({
  title,
  hint,
  standard,
  extra,
}: {
  title: string;
  hint: string;
  standard: string[];
  extra: string[];
}) {
  return (
    <GlassCard className="p-5">
      <h3 className="text-sm font-medium">{title}</h3>
      <p className="mt-1 text-[11px] text-faint">{hint}</p>

      <div className="mt-4">
        <div className="mb-1.5 text-[11px] font-medium text-dim">
          Standard ({standard.length})
        </div>
        <div className="flex flex-wrap gap-1.5">
          {standard.map((c) => (
            <span
              key={c}
              className="rounded-md bg-white/[0.05] px-2 py-1 text-[11px] text-dim ring-1 ring-white/[0.06]"
            >
              {c}
            </span>
          ))}
          {standard.length === 0 && (
            <span className="text-[11px] text-faint">none found in the sample</span>
          )}
        </div>
      </div>

      <div className="mt-4">
        <div className="mb-1.5 text-[11px] font-medium text-accent-300">
          Extra - beyond the standard schema ({extra.length})
        </div>
        <div className="flex flex-wrap gap-1.5">
          {extra.map((c) => (
            <span
              key={c}
              className="rounded-md bg-accent-500/10 px-2 py-1 text-[11px] text-accent-200 ring-1 ring-accent-500/20"
            >
              {c}
            </span>
          ))}
          {extra.length === 0 && (
            <span className="text-[11px] text-faint">
              nothing beyond the standard columns
            </span>
          )}
        </div>
      </div>
    </GlassCard>
  );
}
