const yaml = require("js-yaml");
const path = require("path");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const { CronJob } = require("cron");
const fs = require("fs");
const globby = require("globby");
const { readFileSync, writeFileSync, existsSync } = fs;
const { writeFile, access, readFile } = fs.promises;
const axios = require("axios");
const { callAPI, initialHeader, decrypt } = require("./apiclient");

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

const trySystemJob = new CronJob("1/30 * * * *", async () => {
  logger.info("check update triggered by cron job");
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
    }) === "04:01" ||
    initialHeader["x-data-version"] !== currentVersion.dataVersion ||
    initialHeader["x-asset-version"] !== currentVersion.assetVersion ||
    initialHeader["x-app-version"] !== currentVersion.appVersion
  ) {
    initialHeader["x-app-version"] = currentVersion.appVersion;
    delete initialHeader["x-asset-version"];
    delete initialHeader["x-data-version"];

    await refreshVersions();
    await saveInfoFromSuiteUser();
  } else {
    await refreshInformations();
  }

  await commitMasterDiff({
    dataVersion: initialHeader["x-data-version"],
    assetVersion: initialHeader["x-asset-version"],
  });
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

  logger.debug("write versions to file and add it to git stage area");
  await writeFile(
    path.join(masterDBDiffDir, "versions.json"),
    JSON.stringify(
      { appVersion, dataVersion, assetVersion, assetHash },
      null,
      2
    )
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "versions.json" });

  const master = await callAPI("/suite/master", "get");
  logger.debug("split master into smaller pieces, add them to git stage area");
  for (let key in master) {
    const masterKeyPath = path.join(masterDBDiffDir, `${key}.json`)
    if (key.includes('event')) {
      try {
        await access(masterKeyPath)
        const old = JSON.parse(await readFile(masterKeyPath, { encoding: 'utf8' }))
        if (Array.isArray(old)) {
          master[key] = [...old.filter(o => !master[key].find(m => m.id === o.id)), ...master[key]]
        }
      } catch (err) {
        logger.debug('old event file does not exist')
      }
    }
    await writeFile(
      masterKeyPath,
      JSON.stringify(master[key], null, 2)
    );
    // await git.add({ fs, dir: masterDBDiffDir, filepath: `${key}.json` });
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
      responseType: "arraybuffer",
    }
  );
  await writeFile(
    path.join(masterDBDiffDir, "assetList.json"),
    JSON.stringify(decrypt(Buffer.from(assetList)), null, 2)
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "assetList.json" });

  return { appVersion, dataVersion, assetVersion, assetHash };
}

async function saveInfoFromSuiteUser() {
  const { userId } = account;
  logger.debug("get suite user");
  const userInfo = await callAPI(`/suite/user/${userId}`);

  const { userHomeBanners, userInformations } = userInfo;
  logger.debug("write active homebanners");
  await writeFile(
    path.join(masterDBDiffDir, "userHomeBanners.json"),
    JSON.stringify(userHomeBanners, null, 2)
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "userHomeBanners.json" });

  logger.debug("write active informations");
  await writeFile(
    path.join(masterDBDiffDir, "userInformations.json"),
    JSON.stringify(userInformations, null, 2)
  );
  // await git.add({
  //   fs,
  //   dir: masterDBDiffDir,
  //   filepath: "userInformations.json",
  // });

  return userInfo;
}

async function refreshInformations() {
  logger.debug("get suite user");
  const { informations: userInformations } = await callAPI(`/information`);

  logger.debug("write active informations");
  await writeFile(
    path.join(masterDBDiffDir, "userInformations.json"),
    JSON.stringify(userInformations, null, 2)
  );
}

async function commitMasterDiff(versions) {
  const { GitHubToken } = account;
  const { dataVersion, assetVersion } = versions;
  // const files = await git.listFiles({ fs, dir: masterDBDiffDir });
  const files = await globby([path.relative(__dirname, masterDBDiffDir)])
  let shouldCommit = false;
  for (let filepath of files) {
    const fileStatus = await git.status({ fs, dir: masterDBDiffDir, filepath })
    if (
      fileStatus === "*modified" || fileStatus === "*added" || fileStatus === "absent"
    ) {
      await git.add({ fs, dir: masterDBDiffDir, filepath });
      shouldCommit = true;
    }
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
      ref: "master",
      onAuth: () => ({ username: GitHubToken }),
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
  if (!account.credential) {
    const reg = await registerAccount();

    account.userId = reg.userRegistration.userId;
    account.signature = reg.userRegistration.signature;
    account.credential = reg.credential;

    logger.debug("store new account information");
    await writeFile("account.yaml", yaml.safeDump(account));
  }

  const { userId } = account;
  const {
    appVersion,
    dataVersion,
    assetVersion,
    assetHash,
  } = await refreshVersions();

  logger.info("simulate login process");
  logger.debug("get system");
  await callAPI("/system");
  const { userTutorial } = await saveInfoFromSuiteUser();
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

  logger.info("try commit master db diff if any update");
  await commitMasterDiff({ dataVersion, assetVersion });

  logger.info("all finished, will try for new version every 30 minutes");
  trySystemJob.start();
}

bootstrap();
