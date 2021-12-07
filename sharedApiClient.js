import yaml from "js-yaml";
import log4js from "log4js";
import { readFile, writeFile, stat } from "fs/promises";
import path from "path";
import jwt from "jsonwebtoken";
// import axios from "axios";
// import getTimezoneOffset from "get-timezone-offset";
import uuidV4 from "uuid-v4";
import jayson from "jayson/promise";
import pLimit from "p-limit";
import { CronJob } from "cron";
import { fileURLToPath } from "url";

import { APIClient } from "./apiclient";
import { region } from "./constants";
import { CronetClient } from "./cronet";

const logger = new log4js.getLogger("shared-client");
logger.level = "info";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let apiClient;
let _region;
let isLoggedIn = false;
let suiteUserInfo;

const dayChangeJob = new CronJob(
  "0 4 * * *",
  () => {
    if (isLoggedIn) {
      loginAccount(undefined, true);
    }
  },
  null,
  true,
  "Asia/Tokyo"
);

async function getJPAccount(options = {}) {
  const filePath = path.join(__dirname, "sharedAccount.jp.yaml");
  try {
    await stat(filePath);
    const account = yaml.load(await readFile(filePath, "utf-8"));
    return account;
  } catch (error) {
    logger.warn("no JP shared account found, creating one...");
    const { userRegistration, credential } = await apiClient.registerAccount();
    const { signature } = userRegistration;
    const { userId } = jwt.decode(credential);
    const account = {
      signature,
      credential,
      userId,
    };
    await writeFile(filePath, yaml.dump(account), "utf-8");
    return account;
  }
}

async function getTWAccount(options = {}) {
  if (process.env.SEKAI_TW_ACCESS_TOKEN) {
    return {
      loginInfo: {
        accessToken: process.env.SEKAI_TW_ACCESS_TOKEN,
      },
      userId: process.env.SEKAI_TW_SDK_OPEN_ID,
    };
  }
  const filePath = path.join(__dirname, "sharedAccount.tw.yaml");
  const cronetClient = new CronetClient(logger, options);
  try {
    await stat(filePath);
  } catch (error) {
    logger.warn("no TW shared account found, creating one...");
    const account = {};

    // location query
    const location = await cronetClient.queryLocation();
    logger.info(location);
    // save location in account for further usage
    account.location = location;

    // generate two uuids
    account.loginInfo = {
      clientUUID: uuidV4().toUpperCase(),
      loginID: uuidV4().toUpperCase(),
    };

    // get token
    const { userId, token } = await cronetClient.doVisitorLogin(
      account.loginInfo
    );
    account.loginInfo.userId = userId;
    account.loginInfo.token = token;
    logger.info(account.loginInfo);

    // get accessToken
    const { accessToken, sdkOpenId } = await cronetClient.doLogin(
      account.loginInfo
    );
    account.loginInfo.accessToken = accessToken;
    account.userId = sdkOpenId;
    logger.info(account.loginInfo);

    await writeFile(filePath, yaml.dump(account), "utf-8");
    return account;
  }

  try {
    const account = yaml.load(await readFile(filePath, "utf-8"));
    cronetClient.setLocation(account.location);

    const { userId, token } = await cronetClient.doAutoLogin({
      ...account.loginInfo,
    });
    account.loginInfo.userId = userId;
    account.loginInfo.token = token;

    const { accessToken, sdkOpenId } = await cronetClient.doLogin(
      account.loginInfo
    );
    account.loginInfo.accessToken = accessToken;
    account.userId = sdkOpenId;

    // update account info including token and accessToken
    await writeFile(filePath, yaml.dump(account), "utf-8");
    return account;
  } catch (error) {
    logger.error(error);
  }
}

async function getENAccount(options = {}) {
  const filePath = path.join(__dirname, "sharedAccount.en.yaml");
  try {
    await stat(filePath);
    const account = yaml.load(await readFile(filePath, "utf-8"));
    return account;
  } catch (error) {
    logger.warn("no EN shared account found, creating one...");
    const { userRegistration, credential } = await apiClient.registerAccount();
    const { signature } = userRegistration;
    const { userId } = jwt.decode(credential);
    const account = {
      signature,
      credential,
      userId,
    };
    await writeFile(filePath, yaml.dump(account), "utf-8");
    return account;
  }
}

async function getAccount(options) {
  // apiClient.region = region;
  switch (_region) {
    case "jp":
      return getJPAccount(options);
    case "tw":
      return getTWAccount(options);
    case "en":
      return getENAccount(options);
    default:
      break;
  }
}

async function loginAccount(options, forced = false) {
  try {
    // console.log(isLoggedIn, forced, !isLoggedIn || forced);
    if (!isLoggedIn || forced) {
      dayChangeJob.stop();
      apiClient.account = await getAccount(options);
      const loginRes = await apiClient.login();
      suiteUserInfo = loginRes;
      dayChangeJob.start();

      isLoggedIn = true;
      return loginRes;
    }
  } catch (err) {
    logger.error(err);
  }
  return {};
}

const limit = pLimit(1);

const server = jayson.Server({
  init: async function (args) {
    _region = args[0] || region;
    if (!apiClient || apiClient.region !== _region)
      apiClient = new APIClient(logger, _region);
  },
  login: async function (args) {
    return await limit(() => loginAccount(args[0]));
  },
  relogin: async function (args) {
    return await limit(() => loginAccount(args[0], true));
  },
  checkVersions: async function (args) {
    if (apiClient) {
      return await limit(() => apiClient.checkVersions(args[0]));
    }
    throw server.error(400, "Login before call api endpoint");
  },
  versionInfo: async function () {
    return apiClient.versionInfo;
  },
  account: async function () {
    return apiClient.account;
  },
  callAPI: async function (args) {
    if (apiClient && apiClient.account) {
      return await limit(() => apiClient.callAPI(...args));
    }
    throw server.error(400, "Login before call api endpoint");
  },
  getSuiteUser: async function (args) {
    return suiteUserInfo;
  },
});

/**
 * 39390 = jp
 * 39391 = tw
 * 39392 = en
 * etc
 */
server.http().listen(process.env.PORT || 3939);
