import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Shared dialog shell — click-outside and Escape both close it. Used anywhere a
 * page needs a real form instead of a native `prompt()`/`window.confirm()`. */
export function Modal({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  /** Near-fullscreen card instead of the default compact form width — for content
   * like a raw record dump that needs real reading room, not a form-sized box. */
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="modal-overlay" onClick={onClose}>
      <div className={`neo modal-card ${wide ? "modal-card-wide" : ""}`} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-header">
          <h2>{title}</h2>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body
  );
}
