import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";
import policy from "./policy.example.json" with { type: "json" };

export default defineConfig({
  plugins: [cloudflareTest({
    wrangler: { configPath: "./wrangler.jsonc" },
    miniflare: {
      bindings: {
        // Synthetic test fixture, not a deployment credential.
        ADMIN_SECRET: "test_only_admin_credential_000000000000000000000000",
        POLICY: { ...policy, user_daily_microusd: 100000, user_monthly_microusd: 200000, global_monthly_microusd: 1000000, global_lifetime_microusd: 2000000,
          ingress_per_minute: 10000, global_ingress_per_minute: 10000, user_requests_per_minute: 10000, auth_attempts_per_minute: 10000, admin_attempts_per_minute: 10000 },
      },
    },
  })],
  test: { testTimeout: 15000, hookTimeout: 15000, fileParallelism: false },
});
