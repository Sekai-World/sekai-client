// import yaml from "js-yaml";
import path from "path";
import git from "isomorphic-git";
import http from "isomorphic-git/http/node";
import { CronJob } from "cron";
import axios from "axios";
import { fileURLToPath } from "url";
import fs from "fs";
// const { readFileSync, existsSync, copyFileSync } = fs;
const { writeFile, access, readFile } = fs.promises;
// import axios from "axios";
// import { APIClient, decrypt, assetClient } from "./apiclient";
import { sendEmail, checkGitFolder, clientRequest } from "./utils";
import {
  folders,
  remoteGitBase,
  strapi,
  pjsk,
  region,
  github,
} from "./constants";

import log4js from "log4js";

const logger = log4js.getLogger("check-update");
logger.level = "info";

import jayson from "jayson/promise";

const client = new jayson.Client.http({
  port: process.env.SERVER_PORT || 3939, // change port to use different server
});

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const masterDBDiffDir = path.join(__dirname, folders.masterDBDiff);
const i18nDir = path.join(__dirname, folders.i18n);
const strapiBaseUrl = strapi.baseURL;
const strapiToken = strapi.token;
let versionInfo;

const trySystemJob = new CronJob("1/30 * * * *", async () => {
  logger.info("check update triggered by cron job");
  let verRes;
  try {
    verRes = await clientRequest(client, "checkVersions", [versionInfo]);
  } catch (res) {
    if (res.error.code === 400) {
      // shared api client might get restarted
      await bootstrap();
      verRes = await clientRequest(client, "checkVersions", [versionInfo]);
    }
  }
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

  if (verRes.isNewVersion && pjsk.updateMaster) {
    await refreshVersions();
  }

  if (
    new Date().toLocaleString("en-US", {
      hour: "2-digit",
      hour12: false,
      timeZone: "Asia/Tokyo",
    }) === "04" &&
    pjsk.updateUserInfo
  ) {
    await saveInfoFromSuiteUser();
  }

  if (pjsk.updateUserInfo) {
    await refreshInformations();
  }

  if (
    await commitMasterDiff({
      dataVersion: versionInfo.dataVersion,
      assetVersion: versionInfo.assetVersion,
    })
  ) {
    logger.info("update game content i18n files");
    if (pjsk.updateI18n)
      await commitI18nFiles({
        dataVersion: versionInfo.dataVersion,
        assetVersion: versionInfo.assetVersion,
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
    ref: "main",
    // fastForwardOnly: true,
    author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
    // onAuth: () => ({ username: GitHubToken }),
  });
  if (pjsk.updateI18n)
    await git.pull({
      fs,
      http,
      dir: i18nDir,
      remote: "origin",
      ref: "main",
      // fastForwardOnly: true,
      author: { name: "master-db-diff-bot", email: "anonymous@example.com" },
      // onAuth: () => ({ username: GitHubToken }),
    });

  logger.debug("write versions to file and add it to git stage area");
  versionInfo = await clientRequest(client, "versionInfo", []);
  await writeFile(
    path.join(masterDBDiffDir, "versions.json"),
    JSON.stringify(versionInfo, null, 2)
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "versions.json" });

  const master = await clientRequest(client, "callAPI", [
    "/suite/master",
    "get",
  ]);
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

  // logger.debug("download assets list");
  // const { assetVersion } = apiClient.versionInfo;
  // const { body: assetList } = await assetClient(
  //   `version/${assetVersion}/os/ios`,
  //   {
  //     headers: {
  //       "user-agent": apiClient.headers["user-agent"],
  //       "x-unity-version": apiClient.headers["x-unity-version"],
  //     },
  //   }
  // );
  // await writeFile(
  //   path.join(masterDBDiffDir, "assetList.json"),
  //   JSON.stringify(decrypt(Buffer.from(assetList)), null, 2)
  // );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "assetList.json" });

  return versionInfo;
}

async function saveInfoFromSuiteUser() {
  // const { userId } = account;
  // logger.debug("get suite user");
  const userInfo = await clientRequest(client, "getSuiteUser", []);

  const { userHomeBanners, userInformations } = userInfo;
  logger.debug("write active homebanners");
  await writeFile(
    path.join(masterDBDiffDir, "userHomeBanners.json"),
    JSON.stringify(userHomeBanners, null, 2)
  );
  // await git.add({ fs, dir: masterDBDiffDir, filepath: "userHomeBanners.json" });

  if (userInformations) {
    logger.debug("write active informations");
    await writeFile(
      path.join(masterDBDiffDir, "userInformations.json"),
      JSON.stringify(userInformations, null, 2)
    );
  } else {
    refreshInformations();
  }
  // await git.add({
  //   fs,
  //   dir: masterDBDiffDir,
  //   filepath: "userInformations.json",
  // });

  return userInfo;
}

async function refreshInformations() {
  logger.debug("get suite user");
  const { informations: userInformations } = await clientRequest(
    client,
    "callAPI",
    ["/information"]
  );

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
        if (strapiBaseUrl)
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
        if (strapiBaseUrl)
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
        if (strapiBaseUrl)
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
        if (strapiBaseUrl)
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
  const GitHubToken = github.token;
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
      if (pjsk.updateI18n) await updateI18nFile(filepath);
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
      ref: "main",
      onAuth: () => ({ username: GitHubToken }),
    });
  }

  return shouldCommit;
}

async function commitI18nFiles(versions) {
  const GitHubToken = github.token;
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
  await clientRequest(client, "init", [region]);
  logger.info("check git folders");
  await checkGitFolder(masterDBDiffDir, remoteGitBase);
  if (pjsk.updateI18n) await checkGitFolder(i18nDir, remoteGitBase);

  logger.info("ensure current version available");
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
    versionInfo = await clientRequest(client, "versionInfo", []);

    if (pjsk.updateUserInfo) {
      await clientRequest(client, "login", [region]);
      await saveInfoFromSuiteUser();
    }

    if (pjsk.updateMaster) {
      await refreshVersions();
    }
  } catch (error) {
    logger.error(
      "bootstrap: failed to finish, connection error or account info expired (tw, kr)",
      error
    );
    setTimeout(() => {
      bootstrap();
    }, 10 * 60 * 1000);
    try {
      await sendEmail(
        `Check Update: The connection to project sekai server ${region} failed, please check connection!!!`
      );
      logger.info("update: warning email sent");
    } catch (error) {
      logger.debug("update: skipped email sent");
    }
    return;
  }

  // const versionInfo = await clientRequest(client, "versionInfo", []);
  const { dataVersion, assetVersion } = versionInfo;
  logger.info("try commit master db diff if any update");
  if (await commitMasterDiff({ dataVersion, assetVersion })) {
    logger.info("update game content i18n files");
    if (pjsk.updateI18n) await commitI18nFiles({ dataVersion, assetVersion });
  }

  logger.info("all finished, will try for new version every 30 minutes");
  trySystemJob.start();
}

bootstrap();
