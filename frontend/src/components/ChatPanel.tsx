import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ChatMessage } from "../lib/api";
import "./ChatPanel.css";

/** Slide-in chat panel scoped to one finding. Every answer is grounded only in
 * that finding's own evidence server-side (app/engine/chat) -- this panel just
 * carries the conversation and is honest in the UI when the assistant can't
 * be reached, rather than pretending an answer came back. */
export function ChatPanel({ findingId, onClose }: { findingId: string; onClose: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, sending]);

  async function send() {
    const question = draft.trim();
    if (!question || sending) return;
    setError(null);
    setDraft("");
    const history = messages;
    setMessages([...history, { role: "user", content: question }]);
    setSending(true);
    try {
      const result = await api.chatAboutFinding(findingId, question, history);
      setMessages((prev) => [...prev, { role: "assistant", content: result.answer }]);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? String(err.detail)
          : "Could not reach the chat assistant. Is Ollama running and reachable from the backend?"
      );
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="chat-panel neo">
      <div className="chat-panel-header">
        <h2>Ask about this finding</h2>
        <button type="button" className="modal-close" onClick={onClose} aria-label="Close chat">×</button>
      </div>
      <p className="chat-panel-disclaimer">
        Answers are grounded only in this finding's own evidence — the same data shown on this page.
        It will say it doesn't know rather than guess, and it never delivers a verdict.
      </p>
      <div className="chat-panel-messages" ref={scrollRef}>
        {messages.length === 0 && (
          <p className="coverage-note">
            Ask something like "why was this ranked highly?" or "what's the score?"
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-bubble chat-bubble-${m.role}`}>
            {m.content}
          </div>
        ))}
        {sending && <div className="chat-bubble chat-bubble-assistant chat-bubble-pending">Thinking…</div>}
      </div>
      {error && <p className="chat-panel-error">{error}</p>}
      <div className="chat-panel-input-row">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder="Ask a question about this finding…"
          disabled={sending}
        />
        <button type="button" className="btn-mustard" onClick={send} disabled={sending || !draft.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}
