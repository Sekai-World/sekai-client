// import fs from "fs";
// const { readFileSync, writeFileSync, existsSync } = fs;
// const { writeFile, access, readFile } = fs.promises;
// import crypto from "crypto";
// import uuidV4 from "uuid-v4";
// import jwt from "jsonwebtoken";
// import { CronJob } from "cron";
// import yaml from "js-yaml";
import log4js from "log4js";
import Koa from "koa";
import Router from "@koa/router";
import { clientRequest } from "./utils";
import { region } from "./constants";

const logger = log4js.getLogger("query-as-api");
logger.level = "info";

const app = new Koa();

import jayson from "jayson/promise";

const regionalClientMap = {
  jp: process.env.SERVER_JP_PORT
    ? new jayson.Client.http({
        port: process.env.SERVER_JP_PORT,
      })
    : null,
  tw: process.env.SERVER_TW_PORT
    ? new jayson.Client.http({
        port: process.env.SERVER_TW_PORT,
      })
    : null,
  en: process.env.SERVER_EN_PORT
    ? new jayson.Client.http({
        port: process.env.SERVER_EN_PORT,
      })
    : null,
  kr: process.env.SERVER_EN_PORT
    ? new jayson.Client.http({
        port: process.env.SERVER_EN_PORT,
      })
    : null,
};

async function bootstrap() {
  for (let key in regionalClientMap) {
    const client = regionalClientMap[key];
    if (client) {
      await clientRequest(client, "init", [region]);
      await clientRequest(client, "checkVersions", []);
      await clientRequest(client, "login", []);
    }
  }
}

// const reLoginJob = new CronJob(
//   "0 0 4 * * *",
//   bootstrap,
//   null,
//   false,
//   "Asia/Tokyo"
// );

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

const getRegionalClient = async (ctx, next) => {
  const client = regionalClientMap[ctx.params.region];
  if (!client) {
    ctx.body = {
      status: "error",
      message: "No such region.",
    };
    ctx.status = 400;
    return;
  }
  ctx.client = client;
  return next();
};

const router = new Router();

router.get("/health", async (ctx, next) => {
  const regions = Object.keys(regionalClientMap);
  const isHealthy = regions.some((region) => !!regionalClientMap[region]);
  ctx.body = {
    status: isHealthy ? "success" : "error",
  };
  ctx.status = isHealthy ? 200 : 500;
  return next();
});

const userRouter = new Router();

userRouter.get("/:id/profile", protectedRoute, async (ctx, next) => {
  try {
    const userData = await clientRequest(ctx.client, "callAPI", [
      `/user/${ctx.params.id}/profile`,
    ]);

    ctx.body = {
      status: "success",
      data: userData,
    };
  } catch (error) {
    logger.error(error);
    ctx.body = {
      status: "error",
      message: "check your input.",
    };
    ctx.status = 400;
  }

  return next();
});

userRouter.get(
  "/:id/event/:eventId/ranking",
  protectedRoute,
  async (ctx, next) => {
    try {
      const account = await clientRequest(ctx.client, "account", []);
      const eventRanking = await clientRequest(ctx.client, "callAPI", [
        `/user/${account.userId}/event/${ctx.params.eventId}/ranking?targetUserId=${ctx.params.id}`,
      ]);

      ctx.body = {
        status: "success",
        data: eventRanking,
      };
    } catch (error) {
      logger.error(error);
      ctx.body = {
        status: "error",
        message: "check your input.",
      };
      ctx.status = 400;
    }
    return next();
  }
);

router
  .use("/:region/user", protectedRoute, getRegionalClient, userRouter.routes())
  .use(userRouter.allowedMethods());

router.post(
  "/:region/refresh",
  protectedRoute,
  getRegionalClient,
  async (ctx, next) => {
    await clientRequest(ctx.client, "relogin", []);

    ctx.body = {
      status: "success",
    };

    return next();
  }
);

app.use(router.routes()).use(router.allowedMethods());
app.listen(process.env.PORT || 9393, "localhost");
bootstrap();
