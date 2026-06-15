const regions = ["jp", "en", "cn", "tw", "kr"];

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
