const axios = require("axios");
const msgpack = require("@msgpack/msgpack");
const { initialHeader, baseURL } = require("./constants.js");
const uuidV4 = require("uuid-v4");
const crypto = require("crypto");
const log4js = require("log4js");

const logger = log4js.getLogger('client');
logger.level = "info";

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
  let encrypted = cipher.update(msgpack.encode(body));
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

  return msgpack.decode(decrypted);
}

const myAxios = axios.default.create({
  baseURL,
  transformRequest: [
    (data, headers) => {
      headers["x-request-id"] = uuidV4();
      headers["content-type"] = "application/octet-stream";
      return data ? encrypt(data) : data;
    },
  ],
  responseType: "arraybuffer",
});

myAxios.interceptors.response.use(
  (res) => {
    if (initialHeader["x-session-token"] && res.headers["x-session-token"])
      initialHeader["x-session-token"] = res.headers["x-session-token"];

    res.data = decrypt(Buffer.from(res.data));
    return res;
  },
  async (err) => {
    logger.error(decrypt(Buffer.from(err.response.data)));
    const req = err.config;
    if (err.response.status === 429) {
      // hit rate limit, sleep for a while
      logger.warn("rate limit hit, sleep for 30s");
      await delay(30000);
    }

    return Promise.reject(err);
  }
);

module.exports.callAPI = async function doReq(endpoint, method = "get", body) {
  const { data } = await myAxios({
    url: endpoint,
    method,
    headers: initialHeader,
    data: ["post", "put", "patch"].includes(method) ? body : null,
  });

  return data;
}

module.exports.initialHeader = initialHeader

module.exports.decrypt = decrypt
module.exports.encrypt = encrypt
