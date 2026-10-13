import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Select from "./Select";
import { api, ApiError } from "../lib/api";
import type { CodeSet } from "../lib/types";

export interface CodeSelection {
  /** Parsed, upper-cased, de-duplicated. Empty = use the built-in default set. */
  codes: string[];
  /** The saved set these codes came from, if untouched since loading it. */
  codeSetId: string | null;
}

/** Split whatever was pasted - commas, spaces, newlines, semicolons. */
export function parseCodes(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const tok of text.split(/[\s,;]+/)) {
    const c = tok.trim().toUpperCase();
    if (c && !seen.has(c)) {
      seen.add(c);
      out.push(c);
    }
  }
  return out;
}

/**
 * The CPT/HCPCS codes an extraction pulls, with named sets you can save and
 * reload ("E&M codes", "Cardiology", "Ophthalmology"). Owns its own text and
 * dropdown state and reports the parsed result upward on every change.
 */
export default function CodeSetPanel({
  onChange,
  disabled = false,
  defaultCount = 78,
}: {
  onChange: (sel: CodeSelection) => void;
  disabled?: boolean;
  /** How many codes the built-in default set has, for the blank-state hint. */
  defaultCount?: number;
}) {
  const [sets, setSets] = useState<CodeSet[]>([]);
  const [loadingSets, setLoadingSets] = useState(true);
  const [text, setText] = useState("");
  const [setId, setSetId] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const codes = useMemo(() => parseCodes(text), [text]);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  const reload = useCallback(async () => {
    setLoadingSets(true);
    try {
      setSets(await api.listCodeSets());
    } catch {
      /* the panel still works without saved sets */
    } finally {
      setLoadingSets(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // report upward whenever the effective selection changes
  const key = codes.join(",");
  useEffect(() => {
    onChangeRef.current({ codes, codeSetId: setId || null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, setId]);

  function pickSet(id: string) {
    setSetId(id);
    setMsg(null);
    const s = sets.find((x) => x.id === id);
    setText(s ? s.codes.join(", ") : "");
  }

  function editText(v: string) {
    setText(v);
    // any manual edit makes this a custom list, not the saved set
    if (setId) setSetId("");
    setMsg(null);
  }

  async function save() {
    const name = saveName.trim();
    if (!name || codes.length === 0) return;
    setSaving(true);
    setMsg(null);
    try {
      let saved: CodeSet;
      try {
        saved = await api.createCodeSet(name, codes);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          const existing = sets.find((s) => s.name.toLowerCase() === name.toLowerCase());
          if (
            existing &&
            window.confirm(`A set named "${existing.name}" already exists. Replace its codes?`)
          ) {
            saved = await api.updateCodeSet(existing.id, { codes });
          } else {
            setSaving(false);
            return;
          }
        } else {
          throw e;
        }
      }
      await reload();
      setSetId(saved.id);
      setSaveOpen(false);
      setSaveName("");
      setMsg({ kind: "ok", text: `Saved "${saved.name}" (${saved.codes.length} codes).` });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    const s = sets.find((x) => x.id === setId);
    if (!s || !window.confirm(`Delete the saved set "${s.name}"?`)) return;
    try {
      await api.deleteCodeSet(s.id);
      setSetId("");
      await reload();
      setMsg({ kind: "ok", text: `Deleted "${s.name}". The codes are still in the box below.` });
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    }
  }

  const current = sets.find((s) => s.id === setId);

  return (
    <div>
      <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
        <div>
          <label className="mb-2 block text-[13px] font-medium text-dim">
            Saved code set
          </label>
          <Select
            ariaLabel="Saved CPT/HCPCS code set"
            value={setId}
            disabled={disabled || loadingSets}
            onChange={pickSet}
            options={[
              {
                value: "",
                label: codes.length ? "— custom list —" : "— none: use the default set —",
              },
              ...sets.map((s) => ({
                value: s.id,
                label: s.name,
                hint: `${s.code_count} codes`,
              })),
            ]}
          />
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            className="btn btn-ghost !py-2.5"
            disabled={disabled || codes.length === 0}
            onClick={() => {
              setSaveOpen((o) => !o);
              setSaveName(current?.name ?? "");
            }}
          >
            {saveOpen ? "Cancel" : "Save as set…"}
          </button>
          {current && (
            <button
              type="button"
              className="btn btn-ghost !py-2.5 text-red-300"
              disabled={disabled}
              onClick={remove}
              title="Delete this saved set"
            >
              Delete
            </button>
          )}
        </div>
      </div>

      {saveOpen && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-accent-400/30 bg-accent-600/[0.07] p-3">
          <input
            className="input !w-auto min-w-[220px] flex-1"
            placeholder='Name it, e.g. "E&M codes", "Cardiology"'
            value={saveName}
            autoFocus
            onChange={(e) => setSaveName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
            }}
          />
          <button
            type="button"
            className="btn btn-primary !py-2"
            disabled={saving || !saveName.trim() || codes.length === 0}
            onClick={save}
          >
            {saving ? "Saving…" : `Save ${codes.length} codes`}
          </button>
        </div>
      )}

      <label className="mb-2 mt-4 block text-[13px] font-medium text-dim">
        CPT / HCPCS codes to extract
      </label>
      <textarea
        className="input min-h-[92px] font-mono text-[12.5px] tnum"
        placeholder="Leave blank for the default set. Or paste codes: 99213, 99214, 92014, G0439 …"
        value={text}
        disabled={disabled}
        onChange={(e) => editText(e.target.value)}
        spellCheck={false}
      />
      <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <span className="text-faint tnum">
          {codes.length === 0 ? (
            <>
              Blank &rarr; the built-in default set ({defaultCount} E&amp;M + ophthalmic
              codes).
            </>
          ) : (
            <>
              <span className="text-[var(--text)]">{codes.length} codes</span> &rarr; only
              these are extracted{current ? ` (from "${current.name}")` : ""}.
            </>
          )}
        </span>
        {msg && (
          <span className={msg.kind === "ok" ? "text-accent-400" : "text-red-300"}>
            {msg.text}
          </span>
        )}
      </div>
    </div>
  );
}
