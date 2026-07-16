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
    },
  },
]);

// Standalone simplified checkUpdate-cn process. CN is not a formal service
// region (see D-001): this process runs independently in CHECK_UPDATE_SIMPLE_MODE
// and does not need shared_client/event_tracker peers.
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
  },
});

module.exports = { apps };
