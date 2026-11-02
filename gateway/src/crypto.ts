const encoder = new TextEncoder();

export async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", encoder.encode(value));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}

export function opaqueToken(prefix: "inv" | "ses"): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const encoded = btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
  return `${prefix}_${encoded}`;
}

export function bearer(request: Request): string {
  const header = request.headers.get("Authorization") ?? "";
  if (!/^Bearer [A-Za-z0-9_-]{32,256}$/.test(header)) return "";
  return header.slice(7);
}

export async function adminMatches(supplied: string, expected: string): Promise<boolean> {
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(supplied)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  // Workers' WebCrypto extension compares fixed-length digests in constant time.
  return crypto.subtle.timingSafeEqual(a, b) && supplied.length > 0;
}

export async function peerDigest(request: Request, secret: string): Promise<string> {
  // CF-Connecting-IP is set by Cloudflare at the public ingress. All caller-
  // supplied internal headers are replaced by index.ts. Missing IPs share one
  // bucket. A global rate gate also prevents IP rotation from bypassing limits.
  return digest(`${secret}\npeer\n${request.headers.get("CF-Connecting-IP") ?? "unknown"}`);
}

export async function bodyFingerprint(body: string, secret: string): Promise<string> {
  // A keyed fingerprint avoids storing a publicly testable prompt digest.
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signed = await crypto.subtle.sign("HMAC", key, encoder.encode(`request-body-v1\n${body}`));
  return Array.from(new Uint8Array(signed), b => b.toString(16).padStart(2, "0")).join("");
}
