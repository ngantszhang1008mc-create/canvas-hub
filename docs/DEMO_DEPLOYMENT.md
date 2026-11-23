# Public Sandbox Demo Deployment Guide (AISC1000A)

Canvas Hub is normally a **local-first, single-user companion app** bound strictly to `127.0.0.1` with a private per-installation token and a real Playwright browser for HKUST Canvas SSO/MFA.

For **AISC1000A peer evaluation (Demo Link — Item #5)**, reviewers must be able to test the system directly in their browser **without installing dependencies locally or signing into a personal HKUST Canvas account**. The repository includes a dedicated, containerized **Public Sandbox Demo** mode (`canvas-hub demo-server`) that:

- Pre-seeds a fresh SQLite + FTS5 databank with 4 realistic, de-identified HKUST-style courses (`AISC1000A`, `COMP2011`, `HMAW1905B`, `MATH2111`) across two collection passes (so real deadline revision diffs appear in **Changes**).
- Includes real **Coverage Gaps** (locked Module 6, view-to-complete pledge intentionally not opened, unextracted scanned PDF `Week9_Guest_Lecture_Scanned.pdf`, and `MATH2111` Files not exposed), a synthetic prompt-injection post in the Build Lounge Forum, and a prepared-but-unsent Canvas write preview.
- Supports **three AI backend options** (automatically chosen at startup, with zero-cost showcase conversation pre-seeding):
  1. **Google Vertex AI (Gemini via ADC)** — set `CANVAS_HUB_DEMO_VERTEX_PROJECT` (zero API keys needed on Cloud Run!).
  2. **Any OpenAI / Anthropic-compatible API Key** — set `CANVAS_HUB_DEMO_LLM_API_KEY` + `CANVAS_HUB_DEMO_LLM_BASE_URL` + `CANVAS_HUB_DEMO_LLM_MODEL`.
  3. **Zero-Cost Built-In `DemoToolLLM`** (default when neither env var is set) — executes Canvas Hub's real agent tools locally with zero API cost.
- Enforces **sliding-window AI rate limits** (`429 demo_rate_limited`) to protect paid credits (`CANVAS_HUB_DEMO_AI_RPM_PER_IP=6`, `CANVAS_HUB_DEMO_AI_MAX_PER_HOUR=120`, `CANVAS_HUB_DEMO_AI_MAX_PER_DAY=600`, `CANVAS_HUB_DEMO_MAX_OUTPUT_TOKENS=2048`).
- Locks down browser sign-in, admin settings, backup downloads, outbound crawling, and live Canvas write confirmation (`403 demo_mode_restricted` / `403 demo_write_confirm_disabled`).

---

## Option 1: Google Cloud Run (`asia-east2` Hong Kong) + Vertex AI Gemini (Recommended for GCP Credits)

If you have GCP trial credits (e.g., \$300 free credits), Cloud Run in Hong Kong (`asia-east2`) + native Vertex AI ADC gives **zero cold starts**, **lowest latency for HKUST peers**, and **zero API keys to manage**:

```bash
# 1. Authenticate gcloud (or run directly inside Google Cloud Shell)
gcloud auth login

# 2. One-command build & deploy (enables APIs, creates least-privilege SA, deploys min=1 max=1)
bash scripts/deploy-cloudrun-demo.sh <YOUR_GCP_PROJECT_ID> gemini-3.8-flash
```

- **Why `--min-instances=1 --max-instances=1`**: eliminates cold-start delay for reviewers and keeps the in-container synthetic SQLite database and chat state consistent on a single instance (~USD \$3–\$6 for 2 weeks out of \$300 credits).
- **Cost protection**: Set a Cloud Billing Budget Alert (e.g., \$15) in GCP Console. Built-in rate limits cap traffic at **6 AI messages/min/IP**, **120/hour**, and **600/day** globally, with **2,048 max output tokens** per reply.
- **After grading closes (Dec 10+)**: delete the Cloud Run service to stop all billing:
  ```bash
  gcloud run services delete canvas-hub-demo --project=<YOUR_GCP_PROJECT_ID> --region=asia-east2
  ```

---

## Option 2: Hugging Face Spaces (Docker) or Render.com (No Credit Card)

- **Render.com**: Push this repo to GitHub -> [Render Dashboard](https://dashboard.render.com/) -> **New + → Blueprint** -> select `ngantszhang1008mc-create/canvas-hub` (uses [`render.yaml`](../render.yaml) and [`Dockerfile.demo`](../Dockerfile.demo)).
- **Hugging Face Spaces (Docker SDK)**: Create a Docker Space using `Dockerfile.demo` with `PORT=7860`, and share the direct `https://<user>-<space>.hf.space` URL (not the iframe-wrapped page).

---

## Option 3: Local Machine / VPS + Docker + Cloudflare Tunnel

```bash
# Run locally via uv (zero Docker needed):
uv run canvas-hub demo-server --host 127.0.0.1 --port 8730

# Or run in Docker:
docker compose -f docker-compose.demo.yml up --build -d

# Expose with a quick Cloudflare Tunnel (https://*.trycloudflare.com):
cloudflared tunnel --url http://127.0.0.1:8730
```

---

## Environment Variables Reference

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `CANVAS_HUB_DEMO_VERTEX_PROJECT` | *(unset)* | GCP project ID/number to enable native **Vertex AI (Gemini via ADC)** |
| `CANVAS_HUB_DEMO_VERTEX_LOCATION` | `global` | Vertex AI location (`global`, `us-central1`, `asia-southeast1`, etc.) |
| `CANVAS_HUB_DEMO_VERTEX_MODEL` | `gemini-3.8-flash` | Gemini model ID on Vertex AI |
| `CANVAS_HUB_DEMO_LLM_API_KEY` | *(unset)* | Optional BYOK API key (used if Vertex project is unset) |
| `CANVAS_HUB_DEMO_LLM_PROVIDER` | `openai_compatible` | `openai_compatible` or `anthropic` |
| `CANVAS_HUB_DEMO_LLM_BASE_URL` | `https://api.openai.com/v1` | Upstream OpenAI-compatible base URL |
| `CANVAS_HUB_DEMO_LLM_MODEL` | `gpt-4o-mini` | Upstream model ID |
| `CANVAS_HUB_DEMO_MAX_OUTPUT_TOKENS` | `2048` | Max tokens per assistant reply when using a real LLM |
| `CANVAS_HUB_DEMO_AI_RPM_PER_IP` | `6` | Max AI messages per minute per visitor IP (`0` = unlimited) |
| `CANVAS_HUB_DEMO_AI_MAX_PER_HOUR` | `120` | Global max AI messages per hour across all visitors |
| `CANVAS_HUB_DEMO_AI_MAX_PER_DAY` | `600` | Global max AI messages per 24 hours across all visitors |
