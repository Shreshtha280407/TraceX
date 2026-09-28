/** SSE-over-fetch: native EventSource can't send Authorization headers, so this reads the
 * GET /v1/cases/{id}/events stream manually via fetch()'s ReadableStream. */
import { API_BASE } from "./api";

export type CaseEvent = { id: number; event: string; data: Record<string, unknown> };

export function streamCaseEvents(
  caseId: string,
  token: string | null,
  onEvent: (event: CaseEvent) => void,
  onError?: (error: unknown) => void
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const headers = new Headers({ Accept: "text/event-stream" });
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const response = await fetch(`${API_BASE}/cases/${caseId}/events?follow=true`, {
        headers,
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`SSE connect failed: ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const chunk = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
          if (chunk.startsWith(":")) continue; // heartbeat comment
          let id = 0;
          let event = "message";
          let data = "";
          for (const line of chunk.split("\n")) {
            if (line.startsWith("id: ")) id = Number(line.slice(4));
            else if (line.startsWith("event: ")) event = line.slice(7);
            else if (line.startsWith("data: ")) data += line.slice(6);
          }
          if (data) {
            try {
              onEvent({ id, event, data: JSON.parse(data) });
            } catch {
              /* malformed frame — skip */
            }
          }
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) onError?.(error);
    }
  })();

  return () => controller.abort();
}
