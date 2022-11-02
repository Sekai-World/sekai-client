import axios from "axios";
import msgpack from "@msgpack/msgpack";
import { cookiePostUrl, initialHeader, pjsk, proxy } from "./constants.js";
import uuidV4 from "uuid-v4";
import crypto from "crypto";
import log4js from "log4js";
import SocksProxyAgent from "socks-proxy-agent";
import { HttpsProxyAgent } from "hpagent";
// import { URLSearchParams } from "url";

const apiLogger = log4js.getLogger("client");
apiLogger.level = "info";

// the full socks5 address
// const proxyOptions = `socks5://${proxy.user}:${proxy.pass}@${proxy.host}:${proxy.port}`;
// create the socksAgent for axios
const httpsAgent =
  proxy.type === "socks5"
    ? new SocksProxyAgent({
        host: proxy.host,
        port: proxy.port,
        auth: proxy.user ? `${proxy.user}:${proxy.pass}` : undefined,
        protocol: "socks:",
      })
    : proxy.type === "socks4"
    ? new SocksProxyAgent({
        host: proxy.host,
        port: proxy.port,
        auth: proxy.user ? `${proxy.user}:${proxy.pass}` : undefined,
        protocol: "socks4:",
      })
    : proxy.type === "http"
    ? new HttpsProxyAgent({
        proxy: `http://${proxy.host}:${proxy.port}`,
      })
    : undefined;

function delay(ms) {
  apiLogger.debug(`promise delay for ${ms} ms`);
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function encrypt(body) {
  const cipher = crypto.createCipheriv(
    "aes-128-cbc",
    Buffer.from(
      process.env.AES_KEY || "6732666343305a637a4e394d544a3631",
      "hex"
    ),
    Buffer.from(process.env.AES_IV || "6d737833495630693958453575595a31", "hex")
  );
  let encrypted = cipher.update(body ? msgpack.encode(body) : body);
  encrypted = Buffer.concat([encrypted, cipher.final()]);

  return encrypted;
}

export function decrypt(enc) {
  const cipher = crypto.createDecipheriv(
    "aes-128-cbc",
    Buffer.from(
      process.env.AES_KEY || "6732666343305a637a4e394d544a3631",
      "hex"
    ),
    Buffer.from(process.env.AES_IV || "6d737833495630693958453575595a31", "hex")
  );
  let decrypted = cipher.update(enc);
  decrypted = Buffer.concat([decrypted, cipher.final()]);

  return decrypted.length
    ? msgpack.decode(decrypted)
    : decrypted.toString("hex");
}

export class APIClient {
  constructor(logger, region = "jp") {
    if (!logger) {
      // throw new Error("logger is missing.");
      this.logger = apiLogger;
    } else {
      this.logger = logger;
    }

    this.headers = Object.assign({}, initialHeader[region]);
    // this.logger = logger;
    this.versionInfo = {};
    this._region = region;

    this.isRateLimited = false;

    this.initHttpClient();
  }

  initHttpClient() {
    this.axios = axios.create({
      baseURL: pjsk.baseURL[this._region],
      transformRequest: [
        (data, headers) => {
          headers["x-request-id"] = uuidV4();
          headers["content-type"] = "application/octet-stream";
          return data;
        },
      ],
      responseType: "arraybuffer",
      httpsAgent,
    });

    this.axios.interceptors.response.use(
      (res) => {
        if (res.headers["x-session-token"])
          this.headers["x-session-token"] = res.headers["x-session-token"];

        if (res.data.length) res.data = decrypt(Buffer.from(res.data));
        return res;
      },
      async (err) => {
        // this.logger.error(err.response);
        if (
          err.response.headers["content-type"] === "text/xml" &&
          err.response.status === 403
        ) {
          await this.initCookie();
          throw err;
        }
        if (
          err.response.headers["content-type"] === "application/octet-stream" &&
          err.response.data.length
        ) {
          err.response.data = decrypt(Buffer.from(err.response.data));
        }
        if (err.response.headers["x-session-token"])
          this.headers["x-session-token"] =
            err.response.headers["x-session-token"];
        this.logger.error(err.response.status, err.response.data);
        // const req = err.config;
        if (err.response.status === 429) {
          // hit rate limit, sleep for a while
          this.logger.warn("rate limit hit, sleep for 60s");
          this.isRateLimited = true;
          await delay(60000);
          this.isRateLimited = false;
        } else if (err.response.status === 426) {
          this.logger.warn("should update version");
          const result = await this.checkVersions();
          if (result.isError) {
            this.logger.error("failed to update version");
          } else {
            await this.login();
          }
        } else if (err.response.status === 403) {
          this.logger.warn("unknown error.", err.response.data.errorCode);
          // await this.login();
          if (err.response.data.errorCode === "session_error") {
            await this.login();
          }
        }

        // console.log(err.response)
        throw err;
      }
    );
  }

  async initCookie() {
    const cookieResponse = await axios.post(cookiePostUrl[this.region]);
    const cookie = cookieResponse.headers["set-cookie"];
    this.headers["cookie"] = cookie;
  }

  set account(data) {
    this._account = data;
    // console.log(this._account);
  }

  get account() {
    return this._account;
  }

  set userInfo(data) {
    this._userInfo = data;
  }

  get userInfo() {
    return this._userInfo;
  }

  async callAPI(endpoint, method = "get", body) {
    if (this.isRateLimited) {
      throw new Error("rate limit hit, cooling down.");
    }
    const { data } = await this.axios({
      url: endpoint,
      method,
      headers: this.headers,
      data: ["post", "put", "patch"].includes(method) ? encrypt(body) : null,
    });

    return data;
  }

  async registerAccount() {
    // this.logger.info("create a new account");
    return await this.callAPI("/user", "post", {
      platform: "iOS",
      deviceModel: "iPad6,11",
      operatingSystem: "iOS 13.5",
    });
  }

  async login() {
    this.logger.info("simulate login process");
    this.logger.debug("do auth");
    delete this.headers["x-session-token"];
    // delete this.headers["x-app-version"];
    // delete this.headers["x-data-version"];
    // delete this.headers["x-asset-version"];
    if (this._region === "jp" || this._region === "en") {
      const { userId, credential } = this._account;
      const {
        sessionToken,
        appVersion,
        dataVersion,
        assetVersion,
        assetHash,
        appHash,
        multiPlayVersion,
      } = await this.callAPI(
        `/user/${userId}/auth?refreshUpdatedResources=False`,
        "put",
        {
          credential,
        }
      );

      this.headers["x-session-token"] = sessionToken;
      this.headers["x-app-version"] = appVersion;
      this.headers["x-data-version"] = dataVersion;
      this.headers["x-asset-version"] = assetVersion;
      this.logger.info(
        `login app version ${appVersion} master version ${dataVersion} asset version ${assetVersion}`
      );
    } else if (["tw", "kr"].includes(this._region)) {
      const {
        loginInfo: { accessToken },
        // userId,
      } = this._account;
      const {
        sessionToken,
        appVersion,
        dataVersion,
        assetVersion,
        assetHash,
        appHash,
        multiPlayVersion,
      } = await this.callAPI("/user/auth", "post", {
        userID: 0,
        accessToken,
      });
      // this._account = {
      //   userId,
      // };

      this.headers["x-session-token"] = sessionToken;
      this.headers["x-app-version"] = appVersion;
      this.headers["x-data-version"] = dataVersion;
      this.headers["x-asset-version"] = assetVersion;

      if (["tw", "kr"].includes(this._region)) {
        this.versionInfo = {
          systemProfile: "production",
          appVersion: appVersion,
          multiPlayVersion: multiPlayVersion,
          dataVersion: dataVersion,
          assetVersion: assetVersion,
          appHash: "",
          assetHash: "",
          appVersionStatus: "available",
        };
      }
      this.logger.info(
        `login app version ${appVersion} master version ${dataVersion} asset version ${assetVersion}`
      );
    }

    this.logger.debug("get system");
    await this.callAPI("/system");

    const { userId } = this._account;
    this.logger.debug("get suite user");
    const userInfo = await this.callAPI(`/suite/user/${userId}`);

    // check tutorial status
    const { userTutorial } = userInfo;
    if (userTutorial.tutorialStatus === "start") {
      this.logger.warn("tutorial is at start, set username first");
      await this.callAPI(`/user/${userId}/tutorial`, "patch", {
        tutorialStatus: "opening_1",
      });
      await this.callAPI(`/user/${userId}`, "patch", {
        userGamedata: {
          name: "\u30bb\u30ab\u30a4\u306e\u4f4f\u4eba",
        },
      });
      userTutorial.tutorialStatus = "opening_1";
    }
    // skip tutorial
    if (userTutorial.tutorialStatus !== "end") {
      this.logger.debug("rolling tutorial");
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
        await this.callAPI(`/user/${userId}/tutorial`, "patch", {
          tutorialStatus: status,
        });
      }
    }

    this.logger.debug("refresh home login_bonus");
    await this.callAPI(`/user/${userId}/home/refresh`, "put", {
      refreshableTypes: ["login_bonus"],
    });

    this._userInfo = userInfo;
    return userInfo;
  }

  async getUserProfile(userId) {
    return await this.callAPI(`/user/${userId}/profile`);
  }

  async checkVersions(inputVersion) {
    const res = {
      isError: false,
      isMaintenance: false,
      isNewVersion: false,
    };

    if (["tw", "kr"].includes(this.region)) {
      return res;
    }

    let systemResult;
    try {
      systemResult = await this.callAPI("/system");
    } catch (error) {
      this.logger.error(error);
      res.isError = true;
      return res;
    }
    const allVersions = systemResult.appVersions;
    const appVersion = this.headers["x-app-version"];
    let currentVersion = allVersions.find(
      (allVer) =>
        allVer.appVersion === appVersion &&
        allVer.appVersionStatus === "available"
    );

    if (!currentVersion) {
      // check latest available version
      currentVersion = allVersions.find(
        (allVer) => allVer.appVersionStatus === "available"
      );
      if (currentVersion) {
        res.isNewVersion = true;
      } else {
        currentVersion = allVersions.find(
          (allVer) => allVer.appVersionStatus === "maintenance"
        );
        if (currentVersion) {
          res.isMaintenance = true;
        } else {
          // error
          res.isError = true;
          return res;
        }
      }
    } else {
      res.isNewVersion =
        this.headers["x-data-version"] !== currentVersion.dataVersion ||
        this.headers["x-asset-version"] !== currentVersion.assetVersion ||
        this.headers["x-app-version"] !== currentVersion.appVersion;
    }
    if (res.isNewVersion) {
      this.headers["x-app-version"] = currentVersion.appVersion;
      this.headers["x-asset-version"] = currentVersion.assetVersion;
      this.headers["x-data-version"] = currentVersion.dataVersion;
      this.logger.info(
        `get new version, app version ${currentVersion.appVersion} master version ${currentVersion.dataVersion} asset version ${currentVersion.assetVersion}`
      );
    }
    this.versionInfo = currentVersion;

    if (inputVersion) {
      res.isMaintenance = this.versionInfo.appVersionStatus === "maintenance";
      res.isNewVersion =
        inputVersion.dataVersion !== this.versionInfo.dataVersion ||
        inputVersion.assetVersion !== this.versionInfo.assetVersion ||
        inputVersion.appVersion !== this.versionInfo.appVersion;
      return res;
    }
    if (this.region === "jp") {
      res.isMaintenance = systemResult.maintenanceStatus === "maintenance_in";
    }

    return res;
  }

  set region(newVal) {
    this._region = newVal;

    this.headers = Object.assign({}, initialHeader[newVal]);
    this.initHttpClient();
  }

  get region() {
    return this._region;
  }
}
