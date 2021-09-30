module.exports = {
  initialHeader: {
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
  pjsk: {
    baseURL:
      process.env.SEKAI_API_BASE_URL ||
      "https://production-game-api.sekai.colorfulpalette.org/api",
    assetBaseURL:
      process.env.SEKAI_ASSET_BASE_URL ||
      "https://assetbundle-info.sekai.colorfulpalette.org/api",
    updateMaster: Boolean(process.env.ENABLE_SEKAI_UPDATE_MASTER) || true,
    updateUserInfo: Boolean(process.env.ENABLE_SEKAI_UPDATE_USER_INFO) || false,
    updateI18n: Boolean(process.env.ENABLE_SEKAI_UPDATE_I18N) || false,
  },
  proxy: {
    type: process.env.PROXY_TYPE || "http",
    host: process.env.PROXY_HOST || "localhost",
    port: process.env.PROXY_PORT || 8080,
    user: process.env.PROXY_USER || "",
    pass: process.env.PROXY_PASS || "",
  },
  forceIPv6: Boolean(process.env.ENABLE_IPV6) || false,
  folders: {
    eventTracker: process.env.SEKAI_EVENT_TRACKER || "sekai-event-tracker",
    i18n: process.env.SEKAI_I18N || "sekai-i18n",
    masterDBDiff: process.env.SEKAI_MASTER_DB_DIFF || "sekai-master-db-diff",
  },
  remoteGitBase:
    process.env.REMOTE_GIT_BASE_URL || "https://github.com/Sekai-World",
  strapi: {
    baseURL: process.env.STRAPI_BASE_URL,
    token: process.env.STRAPI_TOKEN,
  },
};
