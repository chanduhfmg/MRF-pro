import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface SelectOption {
  value: string;
  label: string;
  /** dimmed text after the label, e.g. a code count */
  hint?: string;
  disabled?: boolean;
}

/**
 * Dropdown built from real DOM instead of a native <select>.
 *
 * WHY NOT <select>: on Windows, Chrome and Edge paint the option list with a
 * native widget that ignores `option { background-color }` and only partly
 * honours `color-scheme`. On this dark theme that produced a light popup with
 * near-white text - invisible. Styling the CSSOM "correctly" doesn't help,
 * because the menu isn't drawn from it. A DOM listbox is the only way to
 * guarantee the same contrast on every platform (and it can be screenshotted,
 * so the styling is actually testable).
 *
 * The list is rendered through a PORTAL to <body>, not inline. Every card in
 * this app is a transformed, backdrop-filtered element - each one its own
 * stacking context - so an inline menu is painted underneath whichever card
 * comes later in the DOM no matter how high its z-index. A portal plus fixed
 * positioning is the only thing that reliably escapes that.
 *
 * Keyboard: Enter/Space/Arrows open; Arrows move; Enter/Space pick; Escape
 * closes; Home/End jump; typing letters jumps to the next match. Focus stays
 * on the trigger and the active option is tracked with aria-activedescendant,
 * which is what screen readers expect from a collapsed listbox.
 */
export default function Select({
  value,
  onChange,
  options,
  placeholder = "Select…",
  disabled = false,
  className = "",
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [pos, setPos] = useState<{ top: number; left: number; width: number; maxH: number } | null>(
    null,
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const typed = useRef({ text: "", at: 0 });
  const id = useId();

  const selectedIndex = options.findIndex((o) => o.value === value);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : null;

  const close = useCallback(() => {
    setOpen(false);
    setActive(-1);
  }, []);

  // place the portalled list against the trigger; flip up when there is no
  // room below, and keep it glued while the page scrolls
  const place = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const gap = 6;
    const wanted = Math.min(options.length * 46 + 12, 340);
    const below = window.innerHeight - r.bottom - gap - 8;
    const above = r.top - gap - 8;
    const flip = below < wanted && above > below;
    setPos({
      top: flip ? Math.max(8, r.top - gap - Math.min(wanted, above)) : r.bottom + gap,
      left: r.left,
      width: r.width,
      maxH: Math.max(120, Math.min(wanted, flip ? above : below)),
    });
  }, [options.length]);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  // outside click, scroll, resize
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!rootRef.current?.contains(t) && !listRef.current?.contains(t)) close();
    };
    const onScroll = () => place();
    document.addEventListener("mousedown", onDown);
    // capture: a scroll inside any container must reposition, not just window
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, close, place]);

  // keep the active option in view
  useEffect(() => {
    if (!open || active < 0) return;
    listRef.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({
      block: "nearest",
    });
  }, [open, active]);

  function openAt(index: number) {
    setOpen(true);
    setActive(index >= 0 ? index : Math.max(0, selectedIndex));
  }

  function pick(i: number) {
    const o = options[i];
    if (!o || o.disabled) return;
    onChange(o.value);
    close();
    triggerRef.current?.focus();
  }

  function move(delta: number) {
    if (!options.length) return;
    let i = active < 0 ? selectedIndex : active;
    for (let step = 0; step < options.length; step++) {
      i = (i + delta + options.length) % options.length;
      if (!options[i].disabled) break;
    }
    setActive(i);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (disabled) return;
    switch (e.key) {
      case "ArrowDown":
      case "ArrowUp":
        e.preventDefault();
        if (!open) openAt(selectedIndex);
        else move(e.key === "ArrowDown" ? 1 : -1);
        return;
      case "Enter":
      case " ":
        e.preventDefault();
        if (!open) openAt(selectedIndex);
        else if (active >= 0) pick(active);
        return;
      case "Escape":
        if (open) {
          e.preventDefault();
          close();
        }
        return;
      case "Tab":
        if (open) close();
        return;
      case "Home":
      case "End":
        if (open) {
          e.preventDefault();
          setActive(e.key === "Home" ? 0 : options.length - 1);
        }
        return;
    }
    // type-ahead
    if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const now = Date.now();
      typed.current.text = now - typed.current.at > 800 ? e.key : typed.current.text + e.key;
      typed.current.at = now;
      const q = typed.current.text.toLowerCase();
      const from = (active < 0 ? selectedIndex : active) + 1;
      const order = [...options.slice(from), ...options.slice(0, from)];
      const hit = order.find((o) => !o.disabled && o.label.toLowerCase().startsWith(q));
      if (hit) {
        const i = options.indexOf(hit);
        if (!open) openAt(i);
        else setActive(i);
      }
    }
  }

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        className={`input flex w-full items-center justify-between gap-2 text-left disabled:cursor-not-allowed disabled:opacity-55 ${
          open ? "border-accent-400/50 ring-2 ring-accent-600/25" : ""
        }`}
        disabled={disabled}
        onClick={() => (open ? close() : openAt(selectedIndex))}
        onKeyDown={onKeyDown}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        aria-activedescendant={open && active >= 0 ? `${id}-opt-${active}` : undefined}
      >
        <span className={`truncate ${selected ? "" : "text-faint"}`}>
          {selected ? selected.label : placeholder}
          {selected?.hint && <span className="ml-1 text-faint">{selected.hint}</span>}
        </span>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          className={`h-3.5 w-3.5 shrink-0 text-dim transition-transform ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        >
          <path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && pos && createPortal(
        <ul
          ref={listRef}
          role="listbox"
          aria-label={ariaLabel}
          tabIndex={-1}
          style={{
            position: "fixed",
            top: pos.top,
            left: pos.left,
            minWidth: pos.width,
            maxHeight: pos.maxH,
            zIndex: 1000,
          }}
          className="overflow-auto rounded-xl border-2 border-[var(--menu-border)] bg-[var(--menu-bg)] p-1.5 shadow-[0_24px_60px_-12px_rgba(0,0,0,0.9)] ring-1 ring-black/40"
        >
          {options.length === 0 && (
            <li className="px-3 py-2 text-xs text-faint">No options</li>
          )}
          {options.map((o, i) => {
            const isSelected = o.value === value;
            const isActive = i === active;
            return (
              <li
                key={o.value}
                id={`${id}-opt-${i}`}
                data-i={i}
                role="option"
                aria-selected={isSelected}
                aria-disabled={o.disabled || undefined}
                onMouseEnter={() => !o.disabled && setActive(i)}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pick(i)}
                className={`flex cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-[15px] leading-tight transition-colors ${
                  o.disabled
                    ? "cursor-not-allowed text-faint"
                    : isActive
                      ? "bg-[var(--menu-active-bg)] font-medium text-[var(--menu-active-text)] ring-1 ring-inset ring-white/25"
                      : "text-[var(--menu-text)] hover:bg-[var(--menu-hover-bg)]"
                }`}
              >
                <span className="flex min-w-0 items-center gap-2">
                  {/* a left rail on the current value, readable even for
                      someone who can't distinguish the green highlight */}
                  <span
                    className={`h-4 w-[3px] shrink-0 rounded-full ${
                      isSelected ? "bg-accent-400" : "bg-transparent"
                    }`}
                    aria-hidden="true"
                  />
                  <span className={`truncate ${isSelected ? "font-semibold" : ""}`}>{o.label}</span>
                </span>
                <span className="flex shrink-0 items-center gap-2 text-[13px]">
                  {o.hint && (
                    <span className={isActive ? "text-white/80" : "text-dim"}>{o.hint}</span>
                  )}
                  {isSelected && (
                    <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4 text-accent-400" aria-hidden="true">
                      <path
                        d="M5 12.5l4.5 4.5L19 7.5"
                        stroke="currentColor"
                        strokeWidth="2.6"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                </span>
              </li>
            );
          })}
        </ul>,
        document.body,
      )}
    </div>
  );
}
