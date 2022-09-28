// import yaml from "js-yaml";
// import path from "path";
// import git from "isomorphic-git";
// import http from "isomorphic-git/http/node";
import { CronJob } from "cron";
// import { fileURLToPath } from "url";
// import fs from "fs";
// import { writeFile } from "fs/promises";
import axios from "axios";
import { sendEmail, clientRequest } from "./utils";
import { /*folders,*/ region, /*bitbucket,*/ sekaiAPIKey } from "./constants";
import pm2 from "pm2";

import log4js from "log4js";

const logger = log4js.getLogger("event-track");
logger.level = "info";

import jayson from "jayson/promise";

const client = new jayson.Client.http({
  port: process.env.SERVER_PORT || 3939, // change port to use different server
});

// const __filename = fileURLToPath(import.meta.url);
// const __dirname = path.dirname(__filename);
let eventData;
// const eventTrackerDir = path.resolve(__dirname, folders.eventTracker);
// const author = { name: "event-track-bot", email: "anonymous@example.com" };
let versionInfo;

// async function checkVersions() {
//   const res = {
//     isError: false,
//     isMaintenance: false,
//     isNewVersion: false,
//   };
//   let appVersions;
//   try {
//     appVersions = (await clientRequest(client,"callAPI")("/system")).appVersions;
//   } catch (error) {
//     logger.error(error);
//     return {
//       isError: true,
//     };
//   }
//   let currentVersion = appVersions.find(
//     (appVer) =>
//       appVer.appVersion === initialHeader["x-app-version"] &&
//       appVer.appVersionStatus === "available"
//   );

//   if (!currentVersion) {
//     // check latest version
//     currentVersion = appVersions[appVersions.length - 1];
//     if (currentVersion.appVersionStatus === "maintenance") {
//       res.isMaintenance = true;
//     } else if (currentVersion.appVersionStatus === "available") {
//       res.isNewVersion = true;
//       initialHeader["x-app-version"] = currentVersion.appVersion;
//       delete initialHeader["x-asset-version"];
//       delete initialHeader["x-data-version"];
//     }
//   } else {
//     res.isNewVersion =
//       initialHeader["x-data-version"] !== currentVersion.dataVersion ||
//       initialHeader["x-asset-version"] !== currentVersion.assetVersion ||
//       initialHeader["x-app-version"] !== currentVersion.appVersion;
//   }

//   return res;
// }

const eventTrackJob = new CronJob("58 * * * * *", async () => {
  logger.info("trace event score triggered by cron job");
  let verRes;
  try {
    verRes = await clientRequest(client, "checkVersions", [versionInfo]);
  } catch (res) {
    switch (res.code) {
      case "ECONNRESET": {
        pm2.connect((err) => {
          if (err) {
            logger.error(err);
            return;
          }

          pm2.restart(
            process.env.PM2_SHARED_API_CLIENT_PROCESS,
            (err, proc) => {
              if (err) {
                logger.error(err);
                return;
              }

              pm2.disconnect();
            }
          );
        });
        return;
      }
      case 400: {
        // shared api client might get restarted
        await bootstrap();
        verRes = await clientRequest(client, "checkVersions", [versionInfo]);
        break;
      }
      default:
        break;
    }
  }
  // console.log(versionInfo, verRes);
  if (verRes.isMaintenance) {
    logger.warn("server in maintenance");
    return;
  } else if (verRes.isError) {
    logger.error("update: failed to connect server");
    try {
      await sendEmail();
      logger.info("update: warning email sent");
    } catch (error) {
      logger.debug("update: skipped email sent");
    }
    return;
  }

  if (
    verRes.isNewVersion ||
    new Date().toLocaleString("en-DE", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "Asia/Tokyo",
    }) === "04:00"
  ) {
    await refreshVersions();
    // const { userId } = account;
    // await clientRequest(client,"callAPI")(`/suite/user/${userId}`);
  }

  const currentTime = new Date().getTime();
  try {
    await trackEventResult(currentTime);
  } catch (e) {
    // in case of 403 or other errors
    // const { userId } = account;
    await refreshVersions();
    // await clientRequest(client,"callAPI")(`/suite/user/${userId}`);
    await trackEventResult(currentTime);
  }

  // await commitEventTrackResult();
});

const currentEventUrlMap = {
  jp: "https://strapi.sekai.best/sekai-current-event",
  en: "https://strapi.sekai.best/sekai-current-event-en",
  tw: "https://strapi.sekai.best/sekai-current-event-tw",
  kr: "https://strapi.sekai.best/sekai-current-event-kr",
};

async function refreshVersions() {
  logger.info("refersh version info");

  // pull before changes are made
  // await git.pull({
  //   fs,
  //   http,
  //   dir: eventTrackerDir,
  //   remote: "origin",
  //   ref: "main",
  //   // fastForwardOnly: true,
  //   author,
  //   // onAuth: () => ({ username: GitHubToken }),
  // });
  eventData = (await axios.get(currentEventUrlMap[region])).data.eventJson;

  versionInfo = await clientRequest(client, "versionInfo", []);
  return versionInfo;
}

async function trackEventResult(currentTime) {
  if (
    !eventData ||
    currentTime < eventData.startAt ||
    (currentTime > eventData.rankingAnnounceAt + 6 * 60 * 1000 &&
      currentTime < eventData.closedAt - 10 * 1000) ||
    (currentTime > eventData.aggregateAt &&
      currentTime < eventData.rankingAnnounceAt)
  ) {
    logger.warn("No ongoing event, skipping...");
    return;
  } else if (currentTime >= eventData.closedAt - 10 * 1000) {
    logger.warn("current event will expire soon");
    throw Error("current event will expire soon");
  }

  const { userId } = await clientRequest(client, "account", []);

  logger.debug("track first ten");
  const { rankings: first10 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=1&lowerLimit=9`,
  ]);
  logger.debug("track critical ranking");
  const { rankings: rank20 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=20&lowerLimit=0`,
  ]);
  const { rankings: rank30 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=30&lowerLimit=0`,
  ]);
  const { rankings: rank40 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=40&lowerLimit=0`,
  ]);
  const { rankings: rank50 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=50&lowerLimit=0`,
  ]);
  const { rankings: rank100 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=100&lowerLimit=0`,
  ]);
  const { rankings: rank200 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=200&lowerLimit=0`,
  ]);
  const { rankings: rank300 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=300&lowerLimit=0`,
  ]);
  const { rankings: rank400 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=400&lowerLimit=0`,
  ]);
  const { rankings: rank500 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=500&lowerLimit=0`,
  ]);
  const { rankings: rank1000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=1000&lowerLimit=0`,
  ]);
  const { rankings: rank2000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=2000&lowerLimit=0`,
  ]);
  const { rankings: rank3000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=3000&lowerLimit=0`,
  ]);
  const { rankings: rank4000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=4000&lowerLimit=0`,
  ]);
  const { rankings: rank5000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=5000&lowerLimit=0`,
  ]);
  const { rankings: rank10000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=10000&lowerLimit=0`,
  ]);
  const { rankings: rank20000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=20000&lowerLimit=0`,
  ]);
  const { rankings: rank30000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=30000&lowerLimit=0`,
  ]);
  const { rankings: rank40000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=40000&lowerLimit=0`,
  ]);
  const { rankings: rank50000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=50000&lowerLimit=0`,
  ]);
  const { rankings: rank100000 } = await clientRequest(client, "callAPI", [
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=100000&lowerLimit=0`,
  ]);

  // logger.debug("write track result");
  const newData = {
    time: currentTime,
    first10,
    rank20,
    rank30,
    rank40,
    rank50,
    rank100,
    rank200,
    rank300,
    rank400,
    rank500,
    rank1000,
    rank2000,
    rank3000,
    rank4000,
    rank5000,
    rank10000,
    rank20000,
    rank30000,
    rank40000,
    rank50000,
    rank100000,
  };
  // await writeFile(
  //   path.join(eventTrackerDir, `event${eventData.id}.json`),
  //   JSON.stringify(newData, null, 2)
  // );

  // post ranking to api
  try {
    await axios.post(
      `https://api.sekai.best/event/${eventData.id}/rankings`,
      newData,
      {
        headers: {
          "X-API-Key": sekaiAPIKey,
        },
        params: {
          region,
        },
      }
    );
  } catch (e) {
    logger.error("post event ranking to api failed", e);
  }
}

// async function commitEventTrackResult() {
//   const fileStatusMatrix = await git.statusMatrix({ fs, dir: eventTrackerDir });
//   let shouldCommit = false;
//   for (let fileStatus of fileStatusMatrix) {
//     const [filepath, HEAD, WORKDIR, STAGE] = fileStatus;
//     if (
//       (HEAD === 0 && WORKDIR === 2 && STAGE === 0) ||
//       (HEAD === 1 && WORKDIR === 2 && STAGE === 1)
//     ) {
//       await git.add({ fs, dir: eventTrackerDir, filepath });
//       shouldCommit = true;
//     }
//   }
//   if (shouldCommit) {
//     logger.debug("commit and push event track");
//     await git.commit({
//       fs,
//       dir: eventTrackerDir,
//       message: `event track for id ${eventData.id} at ${new Date().getTime()}`,
//       author,
//     });
//     await git.push({
//       fs,
//       http,
//       dir: eventTrackerDir,
//       remote: "origin",
//       ref: "main",
//       onAuth: () => ({
//         username: bitbucket.username,
//         password: bitbucket.token,
//       }),
//     });
//   }

//   return true;
// }

async function bootstrap() {
  await clientRequest(client, "init", [region]);
  logger.info("ensure current version available");
  // await checkGitFolder(eventTrackerDir, remoteGitBase);
  try {
    const verRes = await clientRequest(client, "checkVersions", []);
    if (verRes.isMaintenance) {
      logger.warn("bootstrap: server in maintenance");
      setTimeout(() => {
        bootstrap();
      }, 10 * 60 * 1000);
      return;
    } else if (verRes.isError) {
      logger.warn("bootstrap: server connection error");
      setTimeout(() => {
        bootstrap();
      }, 10 * 60 * 1000);
      return;
    }
  } catch (error) {
    logger.error("bootstrap: failed to connect server", error);
    setTimeout(() => {
      bootstrap();
    }, 10 * 60 * 1000);
    try {
      await sendEmail();
      logger.info("update: warning email sent");
    } catch (error) {
      logger.debug("update: skipped email sent");
    }
    return;
  }

  try {
    await clientRequest(client, "login", []);
    await refreshVersions();
  } catch (error) {
    logger.error("bootstrap: failed to login onto server", error);
    setTimeout(() => {
      bootstrap();
    }, 60 * 60 * 1000);
    try {
      await sendEmail(
        `Event Tracker: The login onto project sekai server ${region} failed, please check parameters!!!`
      );
      logger.info("update: warning email sent");
    } catch (error) {
      logger.debug("update: skipped email sent");
    }
  }

  logger.info("all finished, will track event result every minute");
  if (!eventTrackJob.running) eventTrackJob.start();
}

bootstrap();
