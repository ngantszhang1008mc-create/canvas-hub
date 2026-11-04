import { record } from "./config";
import { GatewayError } from "./errors";

export function protocolError(): never { throw new GatewayError(502, "provider_protocol_error"); }

/** A bounded, one-shot SSE reader: no reconnects, prompt logs, or error bodies. */
export async function* readSse(body: ReadableStream<Uint8Array>, signal: AbortSignal, maxTotalBytes: number, maxEventBytes: number, format: "responses" | "chat" = "responses"): AsyncGenerator<Record<string, unknown>> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
  const encoder = new TextEncoder();
  let buffer = "", eventName = "", data: string[] = [];
  let totalBytes = 0, eventBytes = 0, lines = 0;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (true) {
      signal.throwIfAborted();
      const next = await reader.read();
      signal.throwIfAborted();
      if (next.done) {
        try { buffer += decoder.decode(); } catch { protocolError(); }
        // An unterminated final event is not evidence of final usage.
        if (buffer.trim() || data.length) protocolError();
        return;
      }
      totalBytes += next.value.byteLength;
      if (totalBytes > maxTotalBytes) protocolError();
      try { buffer += decoder.decode(next.value, { stream: true }); } catch { protocolError(); }
      while (true) {
        const match = /[\r\n]/.exec(buffer);
        if (!match) break;
        const end = match.index;
        if (buffer[end] === "\r" && end === buffer.length - 1) break;
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + (buffer.slice(end, end + 2) === "\r\n" ? 2 : 1));
        if (++lines > 131_072) protocolError();
        eventBytes += encoder.encode(line).byteLength + 1;
        if (eventBytes > maxEventBytes) protocolError();
        if (line === "") {
          if (data.length) {
            const raw = data.join("\n");
            if (raw === "[DONE]") yield { type: "transport.done" };
            else {
              let event: unknown;
              try { event = JSON.parse(raw); } catch { protocolError(); }
              if (!record(event)) protocolError();
              if (format === "responses") {
                if (typeof event.type !== "string" || (eventName && eventName !== event.type)) protocolError();
              } else {
                // Chat Completions has no typed Responses events. In particular,
                // JSON must not impersonate the transport's [DONE] sentinel.
                if (event.type !== undefined || (eventName && eventName !== "message")) protocolError();
              }
              yield event;
            }
          }
          data = []; eventName = ""; eventBytes = 0;
        } else if (!line.startsWith(":")) {
          const colon = line.indexOf(":");
          const field = colon === -1 ? line : line.slice(0, colon);
          const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
          if (field === "data") data.push(value);
          else if (field === "event") eventName = value;
          // SSE id/retry are ignored; this adapter never reconnects or retries.
        }
      }
      if (eventBytes + encoder.encode(buffer).byteLength > maxEventBytes) protocolError();
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
