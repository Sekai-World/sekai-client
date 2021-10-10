const yaml = require("js-yaml");
const path = require("path");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const { CronJob } = require("cron");
const fs = require("fs");
const axios = require("axios");
const { readFileSync, copyFileSync, existsSync } = fs;
const { writeFile, readFile } = fs.promises;
const { APIClient } = require("./apiclient");
const { sendEmail, checkGitFolder } = require("./utils");
const { folders, remoteGitBase, strapi, pjsk, region } = require("./constants");

const log4js = require("log4js");

const logger = log4js.getLogger("event-track");
logger.level = "info";

const apiClient = new APIClient(logger, region);

if (!existsSync("./account.yaml")) {
  logger.warn(
    "no account.yaml found, created empty one, remember to fill GitHubToken!"
  );
  copyFileSync(
    path.join(__dirname, "account.example.yaml"),
    path.join(__dirname, "account.yaml")
  );
}
let {
  eventTracker: account,
  BitbucketUser,
  BitbucketToken,
  SekaiAPIKey,
} = yaml.safeLoad(readFileSync("./account.yaml", "utf-8"));
apiClient.account = account;
let eventData;
const eventTrackerDir = path.join(__dirname, folders.eventTracker);
const author = { name: "event-track-bot", email: "anonymous@example.com" };

// async function checkVersions() {
//   const res = {
//     isError: false,
//     isMaintenance: false,
//     isNewVersion: false,
//   };
//   let appVersions;
//   try {
//     appVersions = (await apiClient.callAPI("/system")).appVersions;
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
  const verRes = await apiClient.checkVersions();
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

  const currentTime = new Date().getTime();
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
    // await apiClient.callAPI(`/suite/user/${userId}`);
  }

  try {
    await trackEventResult(currentTime);
  } catch (e) {
    // in case of 403 or other errors
    // const { userId } = account;
    await refreshVersions();
    // await apiClient.callAPI(`/suite/user/${userId}`);
    await trackEventResult(currentTime);
  }

  await commitEventTrackResult();
});

async function refreshVersions() {
  logger.info("refersh version info");

  // pull before changes are made
  await git.pull({
    fs,
    http,
    dir: eventTrackerDir,
    remote: "origin",
    ref: "main",
    // fastForwardOnly: true,
    author,
    // onAuth: () => ({ username: GitHubToken }),
  });

  await apiClient.login();

  const masterData = await apiClient.callAPI("/suite/master", "get");
  const events = masterData.events.filter(
    (it) => it.startAt <= new Date().getTime() + 60 * 1000
  );
  eventData = events.length ? events[events.length - 1] : null;

  return apiClient.versionInfo;
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

  const { userId } = account;

  logger.debug("track first ten");
  const { rankings: first10 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=1&lowerLimit=9`
  );
  logger.debug("track critical ranking");
  const { rankings: rank20 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=20&lowerLimit=0`
  );
  const { rankings: rank30 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=30&lowerLimit=0`
  );
  const { rankings: rank40 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=40&lowerLimit=0`
  );
  const { rankings: rank50 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=50&lowerLimit=0`
  );
  const { rankings: rank100 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=100&lowerLimit=0`
  );
  const { rankings: rank200 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=200&lowerLimit=0`
  );
  const { rankings: rank300 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=300&lowerLimit=0`
  );
  const { rankings: rank400 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=400&lowerLimit=0`
  );
  const { rankings: rank500 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=500&lowerLimit=0`
  );
  const { rankings: rank1000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=1000&lowerLimit=0`
  );
  const { rankings: rank2000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=2000&lowerLimit=0`
  );
  const { rankings: rank3000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=3000&lowerLimit=0`
  );
  const { rankings: rank4000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=4000&lowerLimit=0`
  );
  const { rankings: rank5000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=5000&lowerLimit=0`
  );
  const { rankings: rank10000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=10000&lowerLimit=0`
  );
  const { rankings: rank20000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=20000&lowerLimit=0`
  );
  const { rankings: rank30000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=30000&lowerLimit=0`
  );
  const { rankings: rank40000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=40000&lowerLimit=0`
  );
  const { rankings: rank50000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=50000&lowerLimit=0`
  );
  const { rankings: rank100000 } = await apiClient.callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=100000&lowerLimit=0`
  );

  logger.debug("write track result");
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
  await writeFile(
    path.join(eventTrackerDir, `event${eventData.id}.json`),
    JSON.stringify(newData, null, 2)
  );

  // post ranking to api
  try {
    await axios.default.post(
      `https://api.sekai.best/event/${eventData.id}/rankings`,
      newData,
      {
        headers: {
          "X-API-Key": SekaiAPIKey,
        },
        params: {
          region
        }
      }
    );
  } catch (e) {
    logger.error("post event ranking to api failed");
  }
}

async function commitEventTrackResult() {
  const fileStatusMatrix = await git.statusMatrix({ fs, dir: eventTrackerDir });
  let shouldCommit = false;
  for (let fileStatus of fileStatusMatrix) {
    const [filepath, HEAD, WORKDIR, STAGE] = fileStatus;
    if (
      (HEAD === 0 && WORKDIR === 2 && STAGE === 0) ||
      (HEAD === 1 && WORKDIR === 2 && STAGE === 1)
    ) {
      await git.add({ fs, dir: eventTrackerDir, filepath });
      shouldCommit = true;
    }
  }
  if (shouldCommit) {
    logger.debug("commit and push event track");
    await git.commit({
      fs,
      dir: eventTrackerDir,
      message: `event track for id ${eventData.id} at ${new Date().getTime()}`,
      author,
    });
    await git.push({
      fs,
      http,
      dir: eventTrackerDir,
      remote: "origin",
      ref: "main",
      onAuth: () => ({ username: BitbucketUser, password: BitbucketToken }),
    });
  }

  return true;
}

async function bootstrap() {
  logger.info("ensure current version available");
  await checkGitFolder(eventTrackerDir, remoteGitBase);
  try {
    const verRes = await apiClient.checkVersions();
    if (verRes.isMaintenance) {
      logger.warn("bootstrap: server in maintenance");
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

  await refreshVersions();

  logger.info("all finished, will track event result every minute");
  eventTrackJob.start();
}

bootstrap();
