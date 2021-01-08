const fs = require("fs");
const { readFileSync, writeFileSync, existsSync } = fs;
const { writeFile, access, readFile } = fs.promises;
const crypto = require("crypto");
const uuidV4 = require("uuid-v4");
const jwt = require("jsonwebtoken");
const { CronJob } = require("cron");

const yaml = require("js-yaml");
const log4js = require("log4js");
const Koa = require("koa");
const Router = require("@koa/router");
const { APIClient } = require("./apiclient");

const logger = log4js.getLogger("query-as-api");
logger.level = "info";

const app = new Koa();
const router = new Router();

const max_accounts = process.env.MAX_ALLOW_ACCOUNTS || 5;

const apiClientPool = Array.from({ length: max_accounts }).map(
  () => new APIClient(logger)
);

function delay(ms) {
  logger.debug(`promise delay for ${ms} ms`);
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getClient() {
  let apiClient = apiClientPool[currentPoolIdx++];
  if (currentPoolIdx >= max_accounts) currentPoolIdx = 0;
  while (!apiClient.account) {
    apiClient = apiClientPool[currentPoolIdx++];
    if (currentPoolIdx >= max_accounts) currentPoolIdx = 0;
  }

  return apiClient;
}

async function clientCall(apiClient, endpoint, method = "get", body) {
  try {
    return await apiClient.callAPI(endpoint, method, body);
  } catch (error) {
    if (error.response.status === 426) {
      logger.warn("update api client version");
      await bootstrap();
      return await apiClient.callAPI(endpoint, method, body);
    } else if (error.response.status === 403 && error.response.data.errorCode === "session_error") {
      await apiClient.login();
    }

    throw error;
  }
}

if (!existsSync("./apiClientPool.yaml")) {
  logger.warn("no apiClientPool.yaml found, created empty one!");
  writeFileSync(
    "./apiClientPool.yaml",
    yaml.safeDump(
      Array.from({ length: max_accounts }).map(() => ({
        userId: null,
        signature: null,
        credential: null,
        installId: uuidV4(),
        ai: crypto.randomBytes(16).toString("hex"),
        kc: uuidV4(),
        if: uuidV4().toUpperCase(),
      }))
    )
  );
}
let accounts = yaml.safeLoad(readFileSync("./apiClientPool.yaml", "utf-8"));
let currentPoolIdx = 0;

async function bootstrap() {
  let newAccountCreated = false;
  // prepare account pool
  for (let idx in accounts) {
    const account = accounts[idx];
    apiClientPool[idx].headers["x-install-id"] = account["installId"];
    apiClientPool[idx].headers["x-if"] = account["if"];
    apiClientPool[idx].headers["x-kc"] = account["kc"];
    apiClientPool[idx].headers["x-ai"] = account["ai"];
    await apiClientPool[idx].checkVersions();

    // set if account exists
    if (account && account.userId && account.signature && account.credential) {
      apiClientPool[idx].account = account;
    } else {
      const reg = await apiClientPool[idx].registerAccount();
      logger.info("created a new account");

      // account.userId = reg.userRegistration.userId;
      account.signature = reg.userRegistration.signature;
      account.credential = reg.credential;
      account.userId = jwt.decode(reg.credential).userId;

      apiClientPool[idx].account = account;
      newAccountCreated = true;
    }
    await apiClientPool[idx].login();
    logger.info(`user ${account.userId} logged in`);
    if (newAccountCreated) {
      logger.info("write new accounts to file");
      await writeFile("./apiClientPool.yaml", yaml.safeDump(accounts));
      newAccountCreated = false;
      await delay(60 * 1000);
    }
  }

  logger.info("bootstrap finished!");
}

const reLoginJob = new CronJob(
  "0 0 4 * * *",
  bootstrap,
  null,
  false,
  "Asia/Tokyo"
);

router.get("/health", async (ctx, next) => {
  const isHealth = apiClientPool.some((apiClient) => !!apiClient.account);
  ctx.body = {
    status: isHealth ? "success" : "error",
  };
  ctx.status = isHealth ? 200 : 500;
  next();
});

const protectedRoute = async (ctx, next) => {
  if (ctx.headers["x-api-token"] !== process.env.API_TOKEN) {
    ctx.body = {
      status: "error",
      message: "API Token not found",
    };
    ctx.status = 401;

    return;
  } else {
    return next();
  }
};

router.get("/user/:id/profile", protectedRoute, async (ctx, next) => {
  const apiClient = getClient();

  try {
    const userData = await clientCall(
      apiClient,
      `/user/${ctx.params.id}/profile`
    );

    ctx.body = {
      status: "success",
      data: userData,
    };
  } catch (error) {
    // console.log(error.response.data);
    ctx.body = {
      status: "error",
      message: "check your input.",
    };
    ctx.status = 400;
  }

  return next();
});

router.get(
  "/user/:id/event/:eventId/ranking",
  protectedRoute,
  async (ctx, next) => {
    const apiClient = getClient();

    try {
      const eventRanking = await clientCall(
        apiClient,
        `/user/${apiClient.account.userId}/event/${ctx.params.eventId}/ranking?targetUserId=${ctx.params.id}`
      );

      ctx.body = {
        status: "success",
        data: eventRanking,
      };
    } catch (error) {
      ctx.body = {
        status: "error",
        message: "check your input.",
      };
      ctx.status = 400;
    }
    return next();
  }
);

router.post("/refresh", protectedRoute, async (ctx, next) => {
  for (let apiClient of apiClientPool) {
    await apiClient.login();
  }

  ctx.body = {
    status: "success",
  };
});

app.use(router.routes()).use(router.allowedMethods());
app.listen(process.env.PORT || 9393);
bootstrap().then(() => reLoginJob.start());
