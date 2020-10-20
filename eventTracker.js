const yaml = require("js-yaml");
const path = require("path");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const { CronJob } = require("cron");
const fs = require("fs");
const { readFileSync, writeFileSync, existsSync } = fs;
const { writeFile, readFile } = fs.promises;
const { callAPI, initialHeader } = require("./apiclient");

const log4js = require("log4js");

const logger = log4js.getLogger("event-track");
logger.level = "info";

if (!existsSync("./account.yaml")) {
  logger.warn(
    "no account.yaml found, created empty one, remember to fill GitHubToken!"
  );
  writeFileSync(
    "./account.yaml",
    yaml.safeDump({
      userId: null,
      signature: null,
      credential: null,
      GitHubToken: null,
      BitbucketUser: null,
      BitbucketToken: null,
      event: {
        userId: null,
        credential: null,
      },
    })
  );
}
let { eventTracker: account, BitbucketUser, BitbucketToken } = yaml.safeLoad(
  readFileSync("./account.yaml", "utf-8")
);
let eventData;
const eventTrackerDir = path.join(__dirname, "sekai-event-track");

const eventTrackJob = new CronJob("58 * * * * *", async () => {
  logger.info("trace event score triggered by cron job");
  const { appVersions } = await callAPI("/system");
  const currentVersion = appVersions.find(
    (appVer) =>
      appVer.appVersion === initialHeader["x-app-version"] &&
      appVer.appVersionStatus === "available"
  );
  if (
    !currentVersion ||
    new Date().toLocaleString("en-DE", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "Asia/Tokyo",
    }) === "04:00" ||
    initialHeader["x-data-version"] !== currentVersion.dataVersion ||
    initialHeader["x-asset-version"] !== currentVersion.assetVersion ||
    initialHeader["x-app-version"] !== currentVersion.appVersion
  ) {
    initialHeader["x-app-version"] = currentVersion.appVersion;
    delete initialHeader["x-asset-version"];
    delete initialHeader["x-data-version"];

    await refreshVersions();
    await callAPI(`/suite/user/${userId}`);
  }

  await trackEventResult();

  await commitEventTrackResult();
});

async function refreshVersions() {
  logger.info("refersh version info");
  logger.debug("do auth");
  const { userId, credential } = account;
  const {
    sessionToken,
    appVersion,
    dataVersion,
    assetVersion,
    assetHash,
  } = await callAPI(`/user/${userId}/auth`, "put", {
    credential,
  });
  logger.info(
    `appVersion ${appVersion} dataVersion ${dataVersion} assetVersion ${assetVersion} assetHash ${assetHash}`
  );

  initialHeader["x-session-token"] = sessionToken;
  initialHeader["x-app-version"] = appVersion;
  initialHeader["x-data-version"] = dataVersion;
  initialHeader["x-asset-version"] = assetVersion;

  const masterData = await callAPI("/suite/master", "get");
  eventData = masterData.events.length
    ? masterData.events[masterData.events.length - 1]
    : null;

  return { appVersion, dataVersion, assetVersion, assetHash };
}

async function trackEventResult() {
  const currentTime = new Date().getTime();
  if (
    !eventData ||
    currentTime < eventData.startAt ||
    currentTime > eventData.rankingAnnounceAt + 6 * 60 * 1000 ||
    (currentTime > eventData.aggregateAt &&
      currentTime < eventData.rankingAnnounceAt)
  )
    return;

  const { userId } = account;

  logger.debug("track first ten");
  const { rankings: first10 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=1&lowerLimit=9`
  );
  logger.debug("track critical ranking");
  const { rankings: rank20 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=20&lowerLimit=0`
  );
  const { rankings: rank30 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=30&lowerLimit=0`
  );
  const { rankings: rank40 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=40&lowerLimit=0`
  );
  const { rankings: rank50 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=50&lowerLimit=0`
  );
  const { rankings: rank100 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=100&lowerLimit=0`
  );
  const { rankings: rank200 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=200&lowerLimit=0`
  );
  const { rankings: rank300 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=300&lowerLimit=0`
  );
  const { rankings: rank400 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=400&lowerLimit=0`
  );
  const { rankings: rank500 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=500&lowerLimit=0`
  );
  const { rankings: rank1000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=1000&lowerLimit=0`
  );
  const { rankings: rank2000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=2000&lowerLimit=0`
  );
  const { rankings: rank3000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=3000&lowerLimit=0`
  );
  const { rankings: rank4000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=4000&lowerLimit=0`
  );
  const { rankings: rank5000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=5000&lowerLimit=0`
  );
  const { rankings: rank10000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=10000&lowerLimit=0`
  );
  const { rankings: rank20000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=20000&lowerLimit=0`
  );
  const { rankings: rank30000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=30000&lowerLimit=0`
  );
  const { rankings: rank40000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=40000&lowerLimit=0`
  );
  const { rankings: rank50000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=50000&lowerLimit=0`
  );
  const { rankings: rank100000 } = await callAPI(
    `/user/${userId}/event/${eventData.id}/ranking?targetRank=100000&lowerLimit=0`
  );

  logger.debug("write track result");
  const newData = {
    time: new Date().getTime(),
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
      author: { name: "event-track-bot", email: "anonymous@example.com" },
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
  const { appVersions } = await callAPI("/system");
  const currentVersion = appVersions.find(
    (appVer) =>
      appVer.appVersion === initialHeader["x-app-version"] &&
      appVer.appVersionStatus === "available"
  );
  if (!currentVersion) {
    const availableVersions = appVersions.filter(
      (appVer) => appVer.appVersionStatus === "available"
    );
    initialHeader["x-app-version"] =
      availableVersions[availableVersions.length - 1].appVersion;
  }

  const { userId } = account;
  await refreshVersions();

  logger.info("simulate login process");
  logger.debug("get system");
  await callAPI("/system");
  logger.debug("get suite user");
  const { userTutorial } = await callAPI(`/suite/user/${userId}`);
  if (userTutorial.tutorialStatus === "start") {
    logger.warn("tutorial is at start, set username first");
    await callAPI(`/user/${userId}/tutorial`, "patch", {
      tutorialStatus: "opening_1",
    });
    await callAPI(`/user/${userId}`, "patch", {
      userGamedata: {
        name: "\u30bb\u30ab\u30a4\u306e\u4f4f\u4eba",
      },
    });
    userTutorial.tutorialStatus = "opening_1";
  }
  if (userTutorial.tutorialStatus !== "end") {
    logger.debug("rolling tutorial");
    const steps = [
      "opening_1",
      "gameplay",
      "opening_2",
      "unit_select",
      "idol_opening",
      "summary",
      "end",
    ];
    for (let status of steps.slice(
      steps.indexOf(userTutorial.tutorialStatus) + 1
    )) {
      await callAPI(`/user/${userId}/tutorial`, "patch", {
        tutorialStatus: status,
      });
    }

    logger.debug("refresh home login_bonus");
    await callAPI(`/user/${userId}/home/refresh`, "put", {
      refreshableTypes: ["login_bonus"],
    });
  }

  // await trackEventResult();

  logger.info("all finished, will track event result every minute");
  eventTrackJob.start();
}

bootstrap();
