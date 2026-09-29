import { useState, type FocusEvent, type KeyboardEvent } from "react";

/** A dark-themed dropdown that actually matches the app everywhere, including
 * the open option list -- a native <select>'s closed control can be restyled
 * with CSS, but Chromium renders the expanded option list itself and ignores
 * custom colors on <option>, so it always shows up as a plain white/light
 * list against a dark app regardless of CSS. This replaces the whole control. */
export function ThemedSelect<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const current = options.find((o) => o.value === value);

  function onBlur(event: FocusEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") setOpen(false);
  }

  return (
    <div className="themed-select" onBlur={onBlur} onKeyDown={onKeyDown}>
      <button
        type="button"
        className="themed-select-trigger"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span>{current?.label ?? "—"}</span>
        <span className="themed-select-caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="themed-select-panel" role="listbox" aria-label={ariaLabel}>
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === value}
              className={`themed-select-option ${o.value === value ? "active" : ""}`}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
