const yaml = require("js-yaml");
const path = require("path");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const { CronJob } = require("cron");
const fs = require("fs");
const { readFileSync, existsSync, writeFileSync } = fs;
const { writeFile } = fs.promises;
const axios = require('axios')
const { callAPI, initialHeader } = require("./apiclient")

const log4js = require("log4js");

const logger = log4js.getLogger("check-update");
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
    })
  );
}
let account = yaml.safeLoad(readFileSync("./account.yaml", "utf-8"));
const masterDBDiffDir = path.join(__dirname, "sekai-master-db-diff");

const trySystemJob = new CronJob("1/30 * * * *", () => {
  callAPI("/system").catch((err) => refreshVersions());
});

async function registerAccount() {
  logger.info("create a new account");
  return await callAPI("/user", "post", {
    platform: "iOS",
    deviceModel: "iPad6,11",
    operatingSystem: "iOS 13.5",
  });
}

async function refreshVersions() {
  logger.info("refersh version info");
  logger.debug("do auth");
  const { userId, credential, GitHubToken } = account;
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

  logger.debug("write versions to file and add it to git stage area");
  await writeFile(
    path.join(masterDBDiffDir, "versions.json"),
    JSON.stringify(
      { appVersion, dataVersion, assetVersion, assetHash },
      null,
      2
    )
  );
  await git.add({ fs, dir: masterDBDiffDir, filepath: "versions.json" });

  const master = await callAPI("/suite/master", "get");
  logger.debug("split master into smaller pieces, add them to git stage area");
  for (let key in master) {
    await writeFile(
      path.join(masterDBDiffDir, `${key}.json`),
      JSON.stringify(master[key], null, 2)
    );
    await git.add({ fs, dir: masterDBDiffDir, filepath: `${key}.json` });
  }

  logger.debug("download assets list");
  const { data: assetList } = await axios.get(
    `/version/${assetVersion}/os/ios`,
    {
      baseURL: "https://assetbundle-info.sekai.colorfulpalette.org/api",
      headers: {
        "user-agent": initialHeader["user-agent"],
        "x-unity-version": initialHeader["x-unity-version"],
      },
    }
  );
  await writeFile(
    path.join(masterDBDiffDir, "assetList.json"),
    JSON.stringify(assetList, null, 2)
  );
  await git.add({ fs, dir: masterDBDiffDir, filepath: "assetList.json" });

  const files = await git.listFiles({ fs, dir: masterDBDiffDir });
  let shouldCommit = false;
  for (let filepath of files) {
    if (
      (await git.status({ fs, dir: masterDBDiffDir, filepath })) === "*modified"
    )
      shouldCommit = true;
  }
  if (shouldCommit) {
    logger.debug("commit and push master db diff");
    await git.commit({
      fs,
      dir: masterDBDiffDir,
      message: `master version ${dataVersion} asset version ${assetVersion}`,
      author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    });
    await git.push({
      fs,
      http,
      dir: masterDBDiffDir,
      remote: "origin",
      ref: "main",
      onAuth: () => ({ username: GitHubToken }),
    });
  }
}

async function bootstrap() {
  if (!account.credential) {
    const reg = await registerAccount();

    account.userId = reg.userRegistration.userId;
    account.signature = reg.userRegistration.signature;
    account.credential = reg.credential;

    logger.debug("store new account information");
    await writeFile("account.yaml", yaml.safeDump(account));
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

  logger.info("all finished, will try for new version every 30 minutes");
  trySystemJob.start();
}

bootstrap();
