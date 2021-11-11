// import fs from "fs";
// const { readFileSync, writeFileSync, existsSync } = fs;
// const { writeFile, access, readFile } = fs.promises;
// import crypto from "crypto";
// import uuidV4 from "uuid-v4";
// import jwt from "jsonwebtoken";
import { CronJob } from "cron";
// import yaml from "js-yaml";
import log4js from "log4js";
import Koa from "koa";
import Router from "@koa/router";
import { clientRequest } from "./utils";

const logger = log4js.getLogger("query-as-api");
logger.level = "info";

const app = new Koa();
const router = new Router();

import jayson from "jayson/promise";

const client = new jayson.Client.http({
  port: process.env.SERVER_PORT || 3939, // change port to use different server
});

async function bootstrap() {
  await clientRequest(client, "login", []);
}

const reLoginJob = new CronJob(
  "0 0 4 * * *",
  bootstrap,
  null,
  false,
  "Asia/Tokyo"
);

router.get("/health", async (ctx, next) => {
  const isHealth = true; // apiClientPool.some((apiClient) => !!apiClient.account);
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
  try {
    const userData = await clientRequest(client, "callAPI", [
      `/user/${ctx.params.id}/profile`,
    ]);

    ctx.body = {
      status: "success",
      data: userData,
    };
  } catch (error) {
    logger.error(error.response);
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
    try {
      const account = await clientRequest(client, "account", []);
      const eventRanking = await clientRequest(client, "callAPI", [
        `/user/${account.userId}/event/${ctx.params.eventId}/ranking?targetUserId=${ctx.params.id}`,
      ]);

      ctx.body = {
        status: "success",
        data: eventRanking,
      };
    } catch (error) {
      logger.error(error.response);
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
  await clientRequest(client, "relogin", []);

  ctx.body = {
    status: "success",
  };

  return next();
});

app.use(router.routes()).use(router.allowedMethods());
app.listen(process.env.PORT || 9393);
bootstrap().then(() => reLoginJob.start());
