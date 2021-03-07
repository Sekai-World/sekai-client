const axios = require("axios");
const got = require("got").default;
const msgpack = require("@msgpack/msgpack");
const { initialHeader, baseURL, proxy, assetBaseURL } = require("./constants.js");
const uuidV4 = require("uuid-v4");
const crypto = require("crypto");
const log4js = require("log4js");
const SocksProxyAgent = require("socks-proxy-agent");
const { HttpsProxyAgent } = require("hpagent");

const logger = log4js.getLogger("client");
logger.level = "info";

// the full socks5 address
// const proxyOptions = `socks5://${proxy.user}:${proxy.pass}@${proxy.host}:${proxy.port}`;
// create the socksAgent for axios
const httpsAgent = proxy.type === "socks5" ? new SocksProxyAgent({
  host: proxy.host,
  port: proxy.port,
  auth: proxy.user ? `${proxy.user}:${proxy.pass}` : undefined,
  type: 5
}) : proxy.type === "socks4" ? new SocksProxyAgent({
  host: proxy.host,
  port: proxy.port,
  auth: proxy.user ? `${proxy.user}:${proxy.pass}` : undefined,
  type: 4
}) : proxy.type === "socks5" ? new HttpsProxyAgent({
  proxy: `http://${proxy.host}:${proxy.port}`
}) : undefined;

const rateLimited = false;

function delay(ms) {
  logger.debug(`promise delay for ${ms} ms`);
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function encrypt(body) {
  const cipher = crypto.createCipheriv(
    "aes-128-cbc",
    Buffer.from("6732666343305a637a4e394d544a3631", "hex"),
    Buffer.from("6d737833495630693958453575595a31", "hex")
  );
  let encrypted = cipher.update(body ? msgpack.encode(body) : body);
  encrypted = Buffer.concat([encrypted, cipher.final()]);

  return encrypted;
}

function decrypt(enc) {
  const cipher = crypto.createDecipheriv(
    "aes-128-cbc",
    Buffer.from("6732666343305a637a4e394d544a3631", "hex"),
    Buffer.from("6d737833495630693958453575595a31", "hex")
  );
  let decrypted = cipher.update(enc);
  decrypted = Buffer.concat([decrypted, cipher.final()]);

  return decrypted.length
    ? msgpack.decode(decrypted)
    : decrypted.toString("hex");
}

// const myAxios = axios.default.create({
//   baseURL,
//   transformRequest: [
//     (data, headers) => {
//       headers["x-request-id"] = uuidV4();
//       headers["content-type"] = "application/octet-stream";
//       return data;
//     },
//   ],
//   responseType: "arraybuffer",
//   httpsAgent,
// });

const myClient = got.extend({
  prefixUrl: baseURL,
  responseType: "buffer",
  agent: {
    https: httpsAgent,
  },
  // http2: true,
  hooks: {
    beforeRequest: [
      (options) => {
        options.headers["x-request-id"] = uuidV4();
        options.headers["content-type"] = "application/octet-stream";
      },
    ],
    afterResponse: [
      (response, retryWithMergedOptions) => {
        if (response.statusCode === 429) {
          // hit rate limit, sleep for a while
          logger.warn("rate limit hit, sleep for 30s");
          rateLimited = true;
          setTimeout(() => {
            rateLimited = false;
          }, 30000);

          return response;
        } else {
          if (
            initialHeader["x-session-token"] &&
            response.headers["x-session-token"]
          )
            initialHeader["x-session-token"] = response.headers["x-session-token"];

          if (response.body.length) response.body = decrypt(response.body);

          return response;
        }
      },
    ],
  },
  dnsLookupIpVersion: "ipv6",
});

// myAxios.interceptors.response.use(
//   (res) => {
//     if (initialHeader["x-session-token"] && res.headers["x-session-token"])
//       initialHeader["x-session-token"] = res.headers["x-session-token"];

//     if (res.data.length) res.data = decrypt(Buffer.from(res.data));
//     return res;
//   },
//   async (err) => {
//     if (err.response.data.length) {
//       err.response.data = decrypt(Buffer.from(err.response.data));
//     }
//     logger.error(err.response.status, err.response.data);
//     const req = err.config;
//     if (err.response.status === 429) {
//       // hit rate limit, sleep for a while
//       logger.warn("rate limit hit, sleep for 30s");
//       rateLimited = true;
//       setTimeout(() => {
//         rateLimited = false;
//       }, 30000);
//     }

//     // return Promise.reject(err);
//     throw err;
//   }
// );

/**
 *
 * @param {string} endpoint
 * @param {string} method
 * @param {object} body
 */
module.exports.callAPI = async function doReq(endpoint, method = "get", data) {
  if (endpoint.startsWith("/")) endpoint = endpoint.slice(1);
  const { body } = await myClient(endpoint, {
    method,
    headers: initialHeader,
    body: ["post", "put", "patch"].includes(method) ? encrypt(data) : undefined,
  });

  return body;
};

module.exports.assetClient = got.extend({
  prefixUrl: assetBaseURL,
  responseType: "buffer",
  agent: {
    https: httpsAgent,
  },
  // http2: true,
  dnsLookupIpVersion: "ipv6",
});

module.exports.initialHeader = initialHeader;

module.exports.decrypt = decrypt;
module.exports.encrypt = encrypt;

module.exports.APIClient = class APIClient {
  constructor(logger) {
    if (!logger) {
      throw new Error("logger is missing.");
    }
    this.axios = axios.default.create({
      baseURL,
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

    this.headers = Object.assign({}, initialHeader);
    this.logger = logger;

    this.isRateLimited = false;

    this.axios.interceptors.response.use(
      (res) => {
        if (res.headers["x-session-token"])
          this.headers["x-session-token"] = res.headers["x-session-token"];

        if (res.data.length) res.data = decrypt(Buffer.from(res.data));
        return res;
      },
      async (err) => {
        // console.log(err);
        if (err.response.data.length) {
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
          await this.login();
        } else if (err.response.status === 403) {
          this.logger.warn("unknown error.");
          await this.login();
        }

        // console.log(err.response)

        throw err;
      }
    );
  }

  set account(data) {
    this._account = data;
    // console.log(this._account);
  }

  get account() {
    return this._account;
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
    delete this.headers["x-data-version"];
    delete this.headers["x-asset-version"];
    const { userId, credential } = this._account;
    const {
      sessionToken,
      appVersion,
      dataVersion,
      assetVersion,
    } = await this.callAPI(
      `/user/${userId}/auth?refreshUpdatedResources=False`,
      "put",
      {
        credential,
      }
    );
    // this.logger.info(`user ${userId} logged in`);
    this.headers["x-session-token"] = sessionToken;
    this.headers["x-app-version"] = appVersion;
    this.headers["x-data-version"] = dataVersion;
    this.headers["x-asset-version"] = assetVersion;
    this.logger.info(
      `login app version ${appVersion} master version ${dataVersion} asset version ${assetVersion}`
    );

    this.logger.debug("get system");
    await this.callAPI("/system");

    // const { userId } = this.account;
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

  async checkVersions() {
    const res = {
      isMaintenance: false,
      isNewVersion: false,
    };
    const { appVersions } = await this.callAPI("/system");
    this.logger.debug(appVersions);
    let currentVersion = appVersions.find(
      (appVer) =>
        appVer.appVersion === this.headers["x-app-version"] &&
        appVer.appVersionStatus === "available"
    );

    if (!currentVersion) {
      // check latest version
      currentVersion = appVersions[appVersions.length - 1];
      if (currentVersion.appVersionStatus === "maintence") {
        res.isMaintenance = true;
      } else if (currentVersion.appVersionStatus === "available") {
        res.isNewVersion = true;
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

    return res;
  }
};
