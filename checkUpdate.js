const yaml = require("js-yaml");
const path = require("path");
const git = require("isomorphic-git");
const http = require("isomorphic-git/http/node");
const { CronJob } = require("cron");
const fs = require("fs");
const { readFileSync, writeFileSync, existsSync } = fs;
const { writeFile, access, readFile } = fs.promises;
// const axios = require("axios");
const { APIClient, decrypt, assetClient } = require("./apiclient");
const { sendEmail } = require("./utils");

const log4js = require("log4js");
const { default: axios } = require("axios");

const logger = log4js.getLogger("check-update");
logger.level = "info";

const apiClient = new APIClient(logger);

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
apiClient.account = account;
const masterDBDiffDir = path.join(__dirname, "sekai-master-db-diff");
const i18nDir = path.join(__dirname, "sekai-i18n");
const strapiBaseUrl = process.env.STRAPI_BASE_URL;
const strapiToken = process.env.STRAPI_TOKEN;

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
//     return {
//       isError: true,
//     };
//   }
//   logger.debug(appVersions);
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

const trySystemJob = new CronJob("1/30 * * * *", async () => {
  logger.info("check update triggered by cron job");
  const verRes = await checkVersions();
  if (verRes.isMaintenance) {
    logger.warn("update: server in maintenance");
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
    await saveInfoFromSuiteUser();
  } else {
    await refreshInformations();
  }

  if (
    await commitMasterDiff({
      dataVersion: apiClient.versionInfo.dataVersion,
      assetVersion: apiClient.versionInfo.assetVersion,
    })
  ) {
    logger.info("update game content i18n files");
    await commitI18nFiles({
      dataVersion: apiClient.versionInfo.dataVersion,
      assetVersion: apiClient.versionInfo.assetVersion,
    });
  }
});

async function refreshVersions() {
  logger.info("refersh version info");

  // pull before changes are made
  await git.pull({
    fs,
    http,
    dir: masterDBDiffDir,
    remote: "origin",
    ref: "master",
    // fastForwardOnly: true,
    author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    onAuth: () => ({ username: GitHubToken }),
  });
  await git.pull({
    fs,
    http,
    dir: i18nDir,
    remote: "origin",
    ref: "main",
    // fastForwardOnly: true,
    author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    onAuth: () => ({ username: GitHubToken }),
  });

  logger.debug("write versions to file and add it to git stage area");
  await writeFile(
    path.join(masterDBDiffDir, "versions.json"),
    JSON.stringify(
      apiClient.versionInfo,
      null,
      2
    )
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "versions.json" });

  const master = await apiClient.callAPI("/suite/master", "get");
  logger.debug("split master into smaller pieces, add them to git stage area");
  for (let key in master) {
    const masterKeyPath = path.join(masterDBDiffDir, `${key}.json`);
    if (key.includes("event", "gacha", "virtual", "cheerfulCarnival")) {
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
  const { assetVersion } = apiClient.versionInfo;
  const { body: assetList } = await assetClient(
    `version/${assetVersion}/os/ios`,
    {
      headers: {
        "user-agent": apiClient.headers["user-agent"],
        "x-unity-version": apiClient.headers["x-unity-version"],
      },
    }
  );
  await writeFile(
    path.join(masterDBDiffDir, "assetList.json"),
    JSON.stringify(decrypt(Buffer.from(assetList)), null, 2)
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "assetList.json" });

  return apiClient.versionInfo;
}

async function saveInfoFromSuiteUser() {
  // const { userId } = account;
  // logger.debug("get suite user");
  const userInfo = await apiClient.login();

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
  const { informations: userInformations } = await apiClient.callAPI(`/information`);

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
        await writeFile(
          path.join(i18nDir, "ja", "card_gacha_phrase.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              if (elem.gachaPhrase !== "-") sum[elem.id] = elem.gachaPhrase;
              return sum;
            }, {}),
            null,
            2
          )
        );

        // extra work for updating strapi database
        await axios.post(
          `${strapiBaseUrl}/cards/fromDB?token=${strapiToken}`,
          datas.map((elem) => elem.id)
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

        // extra work for updating strapi database
        await axios.post(
          `${strapiBaseUrl}/musics/fromDB?token=${strapiToken}`,
          datas.map((elem) => elem.id)
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

        // extra work for updating strapi database
        await axios.post(
          `${strapiBaseUrl}/events/fromDB?token=${strapiToken}`,
          datas.map((elem) => elem.id)
        );
      }
      break;

    case "eventStories.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "event_story_episode_title.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              elem.eventStoryEpisodes.forEach((episode) => {
                sum[`${episode.eventStoryId}-${episode.episodeNo}`] =
                  episode.title;
              });
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "honors.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "honor_name.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.name] = elem.name;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "honorGroups.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "honorGroup_name.json"),
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

    case "virtualLives.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "virtualLive_name.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.name;
              return sum;
            }, {}),
            null,
            2
          )
        );

        // extra work for updating strapi database
        await axios.post(
          `${strapiBaseUrl}/virtual-lives/fromDB?token=${strapiToken}`,
          datas.map((elem) => elem.id)
        );
      }
      break;

    case "beginnerMissions.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "beginner_mission.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.sentence;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "honorMissions.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "honor_mission.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.sentence;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "normalMissions.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "normal_mission.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.sentence;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "cheerfulCarnivalSummaries.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "cheerful_carnival_themes.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.theme;
              return sum;
            }, {}),
            null,
            2
          )
        );
      }
      break;

    case "cheerfulCarnivalTeams.json":
      {
        await writeFile(
          path.join(i18nDir, "ja", "cheerful_carnival_teams.json"),
          JSON.stringify(
            datas.reduce((sum, elem) => {
              sum[elem.id] = elem.teamName;
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

  if (!account.credential) {
    const reg = await apiClient.registerAccount();

    account.userId = reg.userRegistration.userId;
    account.signature = reg.userRegistration.signature;
    account.credential = reg.credential;

    logger.debug("store new account information");
    await writeFile("account.yaml", yaml.safeDump(account));

    apiClient.account = account;
  }

  // const { userId } = account;
  await saveInfoFromSuiteUser();
  await refreshVersions();

  // logger.info("simulate login process");
  // logger.debug("get system");
  // await callAPI("/system");
  // const { userTutorial } = await saveInfoFromSuiteUser();
  // if (userTutorial.tutorialStatus === "start") {
  //   logger.warn("tutorial is at start, set username first");
  //   await callAPI(`/user/${userId}/tutorial`, "patch", {
  //     tutorialStatus: "opening_1",
  //   });
  //   await callAPI(`/user/${userId}`, "patch", {
  //     userGamedata: {
  //       name: "\u30bb\u30ab\u30a4\u306e\u4f4f\u4eba",
  //     },
  //   });
  //   userTutorial.tutorialStatus = "opening_1";
  // }
  // if (userTutorial.tutorialStatus !== "end") {
  //   logger.debug("rolling tutorial");
  //   const steps = [
  //     "opening_1",
  //     "gameplay",
  //     "opening_2",
  //     "unit_select",
  //     "idol_opening",
  //     "summary",
  //     "end",
  //   ];
  //   for (let status of steps.slice(
  //     steps.indexOf(userTutorial.tutorialStatus) + 1
  //   )) {
  //     await callAPI(`/user/${userId}/tutorial`, "patch", {
  //       tutorialStatus: status,
  //     });
  //   }

  //   logger.debug("refresh home login_bonus");
  //   await callAPI(`/user/${userId}/home/refresh`, "put", {
  //     refreshableTypes: ["login_bonus"],
  //   });
  // }

  const { dataVersion, assetVersion } = apiClient.versionInfo;
  logger.info("try commit master db diff if any update");
  if (await commitMasterDiff({ dataVersion, assetVersion })) {
    logger.info("update game content i18n files");
    await commitI18nFiles({ dataVersion, assetVersion });
  }

  logger.info("all finished, will try for new version every 30 minutes");
  trySystemJob.start();
}

bootstrap();
