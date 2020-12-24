const yaml = require("js-yaml");
const path = require("path");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const { CronJob } = require("cron");
const fs = require("fs");
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
const i18nDir = path.join(__dirname, "sekai-i18n");

async function checkVersions() {
  const res = {
    isMaintenance: false,
    isNewVersion: false,
  };
  const { appVersions } = await callAPI("/system");
  logger.debug(appVersions);
  let currentVersion = appVersions.find(
    (appVer) =>
      appVer.appVersion === initialHeader["x-app-version"] &&
      appVer.appVersionStatus === "available"
  );

  if (!currentVersion) {
    // check latest version
    currentVersion = appVersions[appVersions.length - 1];
    if (currentVersion.appVersionStatus === "maintence") {
      res.isMaintenance = true;
    } else if (currentVersion.appVersionStatus === "available") {
      res.isNewVersion = true;
      initialHeader["x-app-version"] = currentVersion.appVersion;
      delete initialHeader["x-asset-version"];
      delete initialHeader["x-data-version"];
    }
  } else {
    res.isNewVersion =
      initialHeader["x-data-version"] !== currentVersion.dataVersion ||
      initialHeader["x-asset-version"] !== currentVersion.assetVersion ||
      initialHeader["x-app-version"] !== currentVersion.appVersion;
  }

  return res;
}

const trySystemJob = new CronJob("1/30 * * * *", async () => {
  logger.info("check update triggered by cron job");
  const verRes = await checkVersions();
  if (verRes.isMaintenance) {
    logger.warn("server in maintenance");
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
    await saveInfoFromSuiteUser();
  } else {
    await refreshInformations();
  }

  if (
    await commitMasterDiff({
      dataVersion: initialHeader["x-data-version"],
      assetVersion: initialHeader["x-asset-version"],
    })
  ) {
    logger.info("update game content i18n files");
    await commitI18nFiles({
      dataVersion: initialHeader["x-data-version"],
      assetVersion: initialHeader["x-asset-version"],
    });
  }
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
    const masterKeyPath = path.join(masterDBDiffDir, `${key}.json`);
    if (key.includes("event", "gacha", "virtualLives")) {
      try {
        await access(masterKeyPath);
        const old = JSON.parse(
          await readFile(masterKeyPath, { encoding: "utf8" })
        );
        if (Array.isArray(old)) {
          master[key] = [
            ...old.filter((o) => !master[key].find((m) => m.id === o.id)),
            ...master[key],
          ];
        }
      } catch (err) {
        logger.debug("old event file does not exist");
      }
    }
    await writeFile(masterKeyPath, JSON.stringify(master[key], null, 2));
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

async function updateI18nFile(filepath) {
  if (filepath.includes("/") || !filepath.endsWith(".json")) return;
  const datas = JSON.parse(
    await readFile(path.join(masterDBDiffDir, filepath), {
      encoding: "utf8",
    })
  );

  switch (filepath) {
    case "cards.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "card_prefix.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.prefix;
              return sum;
            }, {}),
            null,
            2
          )
        );
        await writeFile(
          path.join(i18nDir, "ja", "card_skill_name.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.cardSkillName;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "cardEpisodes.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "card_episode_title.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.title] = elem.title;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "musics.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "music_titles.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.title;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "musicVocals.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "music_vocal.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.musicVocalType] = elem.caption;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "stamps.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "stamp_name.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.name
                .replace(/\[.*\]/, "")
                .replace(/^.*：/, "");
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "gachas.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "gacha_name.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.name;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "events.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "event_name.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.name;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;
  }

  return;
}

async function commitMasterDiff(versions) {
  const { GitHubToken } = account;
  const { dataVersion, assetVersion } = versions;
  const fileStatusMatrix = await git.statusMatrix({ fs, dir: masterDBDiffDir });
  let shouldCommit = false;
  // await git.checkout({
  //   fs,
  //   dir: masterDBDiffDir,
  //   remote: "origin",
  //   ref: "master",
  // });
  await git.pull({
    fs,
    http,
    dir: masterDBDiffDir,
    remote: "origin",
    ref: "master",
    fastForwardOnly: true,
    author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    onAuth: () => ({ username: GitHubToken }),
  });
  for (let fileStatus of fileStatusMatrix) {
    const [filepath, HEAD, WORKDIR, STAGE] = fileStatus;
    if (
      (HEAD === 0 && WORKDIR === 2 && STAGE === 0) ||
      (HEAD === 1 && WORKDIR === 2 && STAGE === 1)
    ) {
      await git.add({ fs, dir: masterDBDiffDir, filepath });
      await updateI18nFile(filepath);
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

  return shouldCommit;
}

async function commitI18nFiles(versions) {
  const { GitHubToken } = account;
  const { dataVersion } = versions;
  const fileStatusMatrix = await git.statusMatrix({ fs, dir: i18nDir });
  let shouldCommit = false;
  // await git.checkout({
  //   fs,
  //   dir: i18nDir,
  //   remote: "origin",
  //   ref: "main",
  // });
  await git.pull({
    fs,
    http,
    dir: i18nDir,
    remote: "origin",
    ref: "main",
    fastForwardOnly: true,
    author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    onAuth: () => ({ username: GitHubToken }),
  });
  for (let fileStatus of fileStatusMatrix) {
    const [filepath, HEAD, WORKDIR, STAGE] = fileStatus;
    if (
      (HEAD === 0 && WORKDIR === 2 && STAGE === 0) ||
      (HEAD === 1 && WORKDIR === 2 && STAGE === 1)
    ) {
      await git.add({ fs, dir: i18nDir, filepath });
      shouldCommit = true;
    }
  }
  if (shouldCommit) {
    logger.debug("commit and push i18n files");
    await git.commit({
      fs,
      dir: i18nDir,
      message: `i18n update for master version ${dataVersion}`,
      author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    });
    await git.push({
      fs,
      http,
      dir: i18nDir,
      remote: "origin",
      ref: "main",
      onAuth: () => ({ username: GitHubToken }),
    });
  }

  return shouldCommit;
}

async function bootstrap() {
  logger.info("ensure current version available");
  const verRes = await checkVersions();
  if (verRes.isMaintenance) {
    setTimeout(() => {
      bootstrap();
    }, 10 * 60 * 1000);
    return;
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
  if (await commitMasterDiff({ dataVersion, assetVersion })) {
    logger.info("update game content i18n files");
    await commitI18nFiles({ dataVersion, assetVersion });
  }

  logger.info("all finished, will try for new version every 30 minutes");
  trySystemJob.start();
}

bootstrap();
