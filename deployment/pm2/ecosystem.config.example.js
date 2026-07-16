// Example PM2 ecosystem config (generic template).
//
// This is a TEMPLATE. Copy it to `ecosystem.config.js` on the target host and
// fill in the real values via environment variables (do NOT commit secrets).
//
// Security notes (Phase 2 — see docs/remediation-roadmap.md):
//   * INTERNAL_RPC_TOKEN must be set to the SAME non-empty value in the
//     environment of every formal process (sharedApiClient / checkUpdate /
//     eventTracker / sekai-api-public). It is read dynamically per request and
//     sent as the `x-internal-rpc-token` header over loopback only.
//   * ALLOW_INSECURE_INTERNAL_RPC must NOT be set in production (fail-closed).
//   * sharedAccount.*.yaml must be chmod 0600 (the code enforces this on read;
//     POSIX only).
//   * The Strapi endpoint must accept `Authorization: Bearer <STRAPI_TOKEN>`
//     or `X-Strapi-Token: <STRAPI_TOKEN>`.
//
// Required env (export these before `pm2 start`):
//   INTERNAL_RPC_TOKEN   (shared secret, >=32 random chars)
//   STRAPI_BASE_URL      (e.g. https://strapi.example.com)
//   STRAPI_TOKEN         (Strapi access token)
//   SEKAI_API_KEY        (only needed if tw/kr are enabled)

// Formal service regions. CN is intentionally excluded (not formally deployed;
// only the standalone simplified checkUpdate-cn process below is kept, see D-001).
const regions = ["jp", "en", "tw", "kr"];

const sharedPorts = {
  jp: 39390,
  tw: 39391,
  en: 39392,
  kr: 39393,
  cn: 39394,
};

const apps = regions.flatMap((region) => [
  {
    name: `sharedApiClient-${region}`,
    script: ".venv/bin/gunicorn",
    interpreter: "none",
    cwd: "/root/sekai-client",
    args: `-b 127.0.0.1:${sharedPorts[region]} shared_client:app`,
    env: {
      SEKAI_REGION: region,
      JSONRPC_PORT: String(sharedPorts[region]),
      PYTHONUNBUFFERED: "1",
      INTERNAL_RPC_TOKEN: process.env.INTERNAL_RPC_TOKEN || "",
      STRAPI_BASE_URL: process.env.STRAPI_BASE_URL || "",
      STRAPI_TOKEN: process.env.STRAPI_TOKEN || "",
      // ALLOW_INSECURE_INTERNAL_RPC intentionally omitted (fail-closed).
    },
  },
  {
    name: `checkUpdate-${region}`,
    script: "check_update.py",
    interpreter: ".venv/bin/python",
    cwd: "/root/sekai-client",
    env: {
      SEKAI_REGION: region,
      JSONRPC_PORT: String(sharedPorts[region]),
      PYTHONUNBUFFERED: "1",
      INTERNAL_RPC_TOKEN: process.env.INTERNAL_RPC_TOKEN || "",
      STRAPI_BASE_URL: process.env.STRAPI_BASE_URL || "",
      STRAPI_TOKEN: process.env.STRAPI_TOKEN || "",
    },
  },
  {
    name: `eventTracker-${region}`,
    script: "event_tracker.py",
    interpreter: ".venv/bin/python",
    cwd: "/root/sekai-client",
    env: {
      SEKAI_REGION: region,
      JSONRPC_PORT: String(sharedPorts[region]),
      PYTHONUNBUFFERED: "1",
      INTERNAL_RPC_TOKEN: process.env.INTERNAL_RPC_TOKEN || "",
      STRAPI_BASE_URL: process.env.STRAPI_BASE_URL || "",
      STRAPI_TOKEN: process.env.STRAPI_TOKEN || "",
    },
  },
]);

// Standalone simplified checkUpdate-cn process. CN is not a formal service
// region (see D-001): this process runs independently in CHECK_UPDATE_SIMPLE_MODE
// and does not need shared_client/event_tracker peers, so it does not require
// INTERNAL_RPC_TOKEN / loopback RPC auth.
apps.push({
  name: "checkUpdate-cn",
  script: "check_update.py",
  interpreter: ".venv/bin/python",
  cwd: "/root/sekai-client",
  env: {
    SEKAI_REGION: "cn",
    JSONRPC_PORT: String(sharedPorts.cn),
    CHECK_UPDATE_SIMPLE_MODE: "true",
    PYTHONUNBUFFERED: "1",
    INTERNAL_RPC_TOKEN: process.env.INTERNAL_RPC_TOKEN || "",
    STRAPI_BASE_URL: process.env.STRAPI_BASE_URL || "",
    STRAPI_TOKEN: process.env.STRAPI_TOKEN || "",
  },
});

apps.push({
  name: "sekai-api-public",
  script: ".venv/bin/gunicorn",
  interpreter: "none",
  cwd: "/root/sekai-client",
  args: "-b 127.0.0.1:39400 api_public_server:app",
  env: {
    PYTHONUNBUFFERED: "1",
    INTERNAL_RPC_TOKEN: process.env.INTERNAL_RPC_TOKEN || "",
    STRAPI_BASE_URL: process.env.STRAPI_BASE_URL || "",
    STRAPI_TOKEN: process.env.STRAPI_TOKEN || "",
  },
});

module.exports = { apps };
