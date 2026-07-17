// Formal service regions. CN is intentionally excluded (not formally deployed;
// only the standalone simplified checkUpdate-cn process below is kept, see D-001).
//
// Export secrets before starting/reloading PM2; do not put literal secrets here:
//   INTERNAL_RPC_TOKEN  Shared by all formal services; use one non-empty value.
//   API_TOKEN           Required by sekai-api-public and the Dashboard.
//   STRAPI_BASE_URL     Used by JP checkUpdate and all current eventTrackers.
//   STRAPI_TOKEN        Used only by JP checkUpdate; sent in auth headers.
//   SEKAI_API_KEY       Required by eventTracker for regions that use Sekai API.
//
// Production intentionally does not pass ALLOW_INSECURE_INTERNAL_RPC or
// ENABLE_UNSAFE_PJSK_RPC. Internal RPC therefore remains fail-closed.
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
      // Only JP publishes i18n IDs to Strapi in this deployment.
      ...(region === "jp"
        ? {
            STRAPI_BASE_URL: process.env.STRAPI_BASE_URL || "",
            STRAPI_TOKEN: process.env.STRAPI_TOKEN || "",
          }
        : {}),
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
      SEKAI_API_KEY: process.env.SEKAI_API_KEY || "",
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
    API_TOKEN: process.env.API_TOKEN || "",
  },
});

module.exports = { apps };
