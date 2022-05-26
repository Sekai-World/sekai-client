export const initialHeader = {
  jp: {
    "x-devicemodel": "iPad6,11",
    "x-app-version": process.env.APP_VER || "1.10.0",
    // "x-asset-version": '1.0.0',
    // "x-data-version": '1.0.4',
    "x-if": "AB5D5811-551F-4590-A671-093BEB2381E2",
    "x-install-id": "5715782a-f05a-4fda-a853-35b1184e4912",
    "x-kc": "0bdf6de1-0a00-43e3-a632-c1af30b9484a",
    "x-operatingsystem": "iOS 13.5",
    "x-platform": "iOS",
    "x-unity-version": "2019.4.3f1",
    "x-ai": "",
    "x-ma": "",
    "x-ga": "",
    "user-agent": "pjsekai/26 CFNetwork/1126 Darwin/19.5.0",
    Accept: "application/octet-stream",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "zh-cn",
    // "content-type": 'application/octet-stream'
  },
  tw: {
    "X-Unity-Version": "2019.4.19f1c1",
    "X-DeviceModel": "iPad6,11",
    Accept: "application/octet-stream",
    "X-Install-Id": "9ed9f6fb-4159-4844-842c-26bec8e7289b",
    "Accept-Language": "zh-cn",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type": "application/octet-stream",
    // 'X-Request-Id': '8c3aa6b3-a505-4974-afca-b2e911c85434',
    "User-Agent":
      "%E4%B8%96%E7%95%8C%E8%A8%88%E7%95%AB/1258 CFNetwork/1126 Darwin/19.5.0",
    device_id: process.env.SEKAI_TW_DEVICE_ID || "7013473306716718998",
    "X-App-Version": process.env.APP_VER || "1.7.3",
    "X-Platform": "iOS",
    "X-OperatingSystem": "iOS 13.5",
  },
  en: {
    "x-devicemodel": "iPad6,11",
    "x-app-version": process.env.APP_VER || "1.10.0",
    // "x-asset-version": '1.0.0',
    // "x-data-version": '1.0.4',
    "x-if": "AB5D5811-551F-4590-A671-093BEB2381E2",
    "x-install-id": "5715782a-f05a-4fda-a853-35b1184e4912",
    "x-kc": "0bdf6de1-0a00-43e3-a632-c1af30b9484a",
    "x-operatingsystem": "iOS 13.5",
    "x-platform": "iOS",
    "x-unity-version": "2019.4.3f1",
    "x-ai": "",
    "x-ma": "",
    "x-ga": "",
    "user-agent": "pjsekai/26 CFNetwork/1126 Darwin/19.5.0",
    Accept: "application/octet-stream",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "zh-cn",
    // "content-type": 'application/octet-stream'
  },
  kr: {
    "X-Unity-Version": "2019.4.19f1c1",
    "X-DeviceModel": "iPad6,11",
    Accept: "application/octet-stream",
    "X-Asset-Version": "",
    "X-Install-Id": "3e9d5364-1c68-4f53-aae8-2824e08e993f",
    "X-Data-Version": "",
    "User-Agent":
      "%ED%94%84%EB%A1%9C%EC%84%B8%EC%B9%B4/5011 CFNetwork/1126 Darwin/19.5.0",
    "X-App-Version": process.env.APP_VER || "1.9.9",
    // "X-Session-Token": "",
    "X-Platform": "iOS",
    "X-OperatingSystem": "iOS 13.5",
    device_id: process.env.SEKAI_KR_DEVICE_ID || "7013473306716718593",
  },
};
export const pjsk = {
  baseURL: {
    jp: "https://production-game-api.sekai.colorfulpalette.org/api",
    tw: "https://mk-zian-obt-https.bytedgame.com/api",
    en: "https://n-production-game-api.sekai-en.com/api",
    kr: "https://mkkorea-obt-prod01-cdn.bytedgame.com/api",
  },
  // assetBaseURL:
  //   process.env.SEKAI_ASSET_BASE_URL ||
  //   "https://assetbundle-info.sekai.colorfulpalette.org/api",
  updateMaster: Boolean(process.env.ENABLE_SEKAI_UPDATE_MASTER) || true,
  updateUserInfo: Boolean(process.env.ENABLE_SEKAI_UPDATE_USER_INFO) || false,
  updateI18n: Boolean(process.env.ENABLE_SEKAI_UPDATE_I18N) || false,
};
export const proxy = {
  type: process.env.PROXY_TYPE || "none",
  host: process.env.PROXY_HOST || "localhost",
  port: process.env.PROXY_PORT || 8080,
  user: process.env.PROXY_USER || "",
  pass: process.env.PROXY_PASS || "",
};
export const forceIPv6 = Boolean(process.env.ENABLE_IPV6) || false;
export const folders = {
  eventTracker: process.env.SEKAI_EVENT_TRACKER || "sekai-event-track",
  i18n: process.env.SEKAI_I18N || "sekai-i18n",
  masterDBDiff: process.env.SEKAI_MASTER_DB_DIFF || "sekai-master-db-diff",
};
export const remoteGitBase =
  process.env.REMOTE_GIT_BASE_URL || "https://github.com/Sekai-World";
export const strapi = {
  baseURL: process.env.STRAPI_BASE_URL,
  token: process.env.STRAPI_TOKEN,
};
export const region = process.env.SEKAI_REGION || "jp";
export const github = { token: process.env.GITHUB_TOKEN };
export const bitbucket = {
  username: process.env.BITBUCKET_USERNAME,
  token: process.env.BITBUCKET_TOKEN,
};
export const sekaiAPIKey = process.env.SEKAI_API_KEY;
