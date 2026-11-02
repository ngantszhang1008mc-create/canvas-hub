export class GatewayError extends Error {
  constructor(public status: number, public code: string, public requestId?: string) {
    // Never put user input, provider errors, or credentials into exceptions.
    super(code);
  }
}

export const responseHeaders = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};

export function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: responseHeaders });
}

export function errorResponse(error: unknown): Response {
  const e = error instanceof GatewayError ? error : new GatewayError(500, "internal_error");
  return json({ error: e.code, ...(e.requestId ? { request_id: e.requestId } : {}) }, e.status);
}
