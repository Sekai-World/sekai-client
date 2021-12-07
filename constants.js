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
};
export const twSDK = {
  params: {
    app_version_minor: process.env.APP_VER || "1.7.3",
    device_id: process.env.SEKAI_TW_DEVICE_ID || "7013473306716718998", // filled with registered device id
    os_version: "13.5",
    device_model: "iPad6,11",
    shark_extra: JSON.stringify({
      gsdk_version_code: process.env.SEKAI_TW_SDK_VERSION || "3.7.0.3",
    }),
    channel_op: "App Store",
    iid: process.env.SEKAI_TW_IID || "7013477617354238721",
    app_name: "pjsk_oversea",
    sdk_language: "zh-Hant",
    _rticket: "", // timestamp in millisecond
    sdk_version: process.env.SEKAI_TW_SDK_VERSION || "3.7.0.3",
    ac: "wifi",
    // !!! stringify
    custom: {
      real_package_name: "com.hermes.mk.asia",
      login_way: "guest",
      gm_patch_version: "",
      userid_b: "", // fill when not visitor_login
      build_number: "1258",
      device_platform: "iphone",
      user_is_login: 0,
      appsflyer_id: "1632952514070-9562372",
      gsdk_version: process.env.SEKAI_TW_SDK_VERSION || "3.7.0.3",
      ban_odin: 1,
      province: "", // use query result
      unique_id: "",
      city: "", // use query result
      app_version_minor: process.env.APP_VER || "1.7.3",
      version_code: process.env.APP_VER || "1.7.3",
      country: "", // use query result
      channel_op: "App Store",
      environment: "online",
    },
    version_code: process.env.APP_VER || "1.7.3",
    vid: "13747692-768d-4b1e-a94c-469a81e7b009".toUpperCase(),
    channel: "App Store",
    screen_height: "2048",
    appsflyer_id: "1632952514070-9562372",
    idfa: "60bc77dd-1137-47d3-a16b-6b9c02c306e6".toUpperCase(),
    device_platform: "iphone",
    device_type: "iPad6,11",
    os: "iOS",
    dpi: "768",
    tz_name: "", // use query result
    build_number: process.env.SEKAI_TW_BUILD_NUMBER || "1258",
    tz_offset: "", // use query result
    aid: "5245",
    screen_width: "1536",
    // !!! stringify with 2 spaces
    intl_info: {
      app_region: "JP",
      gps_country: "", // use query result, lowercase
      sys_language: "en",
      carrier_region: "",
      gps_city: "", // use query result
      timezone_name: "", // use query result
      sys_region: "JP",
      app_language: "en",
    },
    sdk_app_id: "1782",
    language: "zh-Hant",
    app_package: "com.hermes.mk.asia",
    cdid: "66037373-b85d-41ac-9138-9ad0a6123899".toUpperCase(),
    app_version: process.env.APP_VER || "1.7.3",
    device_name: "WTFIPAD",
    resolution: "1536*2048",
  },
  data: {
    visitorLogin: {
      _rticket: "", // from params
      ac: "wifi",
      aid: "5245",
      app_name: "pjsk_oversea",
      app_package: "com.hermes.mk.asia",
      app_version: "", // from params
      app_version_minor: "", // from params
      appsflyer_id: "", // from params
      build_number: "", // from params
      cdid: "", // from params
      channel: "App Store",
      channel_op: "App Store",
      client_uuid: "", // uuidV4, uppercase
      custom: "", // from params
      device_id: "", // from params
      device_model: "iPad6,11",
      device_name: "", // from params
      device_platform: "iphone",
      device_type: "iPad6,11",
      dpi: "768",
      idfa: "", // from params
      iid: "", // from params
      intl_info: "", // from params
      is_create: "0",
      language: "zh-Hant",
      login_id: "", // uuidV4, uppercase
      login_way: "guest",
      os: "iOS",
      os_version: "13.5",
      resolution: "1536*2048",
      screen_height: "2048",
      screen_width: "1536",
      sdk_app_id: "1782",
      sdk_language: "zh-Hant",
      sdk_version: "", // from params
      shark_extra: "", // from params
      tz_name: "", // from params
      tz_offset: "", // from params
      ui_flag: "1",
      user_type: "1",
      version_code: "", // from params
      vid: "", // from params
    },
    autoLogin: {
      _rticket: "",
      ac: "wifi",
      access_token: "",
      aid: "5245",
      app_name: "pjsk_oversea",
      app_package: "com.hermes.mk.asia",
      app_version: "",
      app_version_minor: "",
      appsflyer_id: "",
      build_number: "",
      cdid: "",
      channel: "App Store",
      channel_op: "App Store",
      client_uuid: "",
      custom: "",
      device_id: "",
      device_model: "iPad6,11",
      device_name: "",
      device_platform: "iphone",
      device_type: "iPad6,11",
      dpi: "768",
      idfa: "",
      iid: "",
      intl_info: "",
      is_sc_login: "0",
      language: "zh-Hant",
      login_id: "",
      login_way: "guest",
      os: "iOS",
      os_version: "13.5",
      resolution: "1536*2048",
      screen_height: "2048",
      screen_width: "1536",
      sdk_app_id: "1782",
      sdk_language: "zh-Hant",
      sdk_version: "",
      shark_extra: "",
      token: "",
      tz_name: "",
      tz_offset: "",
      ui_flag: "1",
      user_id: "",
      user_type: "1",
      version_code: "",
      vid: "",
    },
    login: {
      access_token: "", // delete if not given
      activation_code: "",
      aid: "5245",
      channel_id: "bsdkintl",
      client_uuid: "",
      // !!! stringify with 2 spaces
      data: {
        user_id: "", // user_id from visitorLogin or autoLogin
        token: "", // token from visitorLogin or autoLogin
      },
      device_id: "",
      iid: "",
      login_id: "",
      login_way: "guest",
      sdk_version: "",
      ui_flag: "1",
    },
  },
  headers: {
    "User-Agent": "ä¸çè¨ç« 1.7.3 rv:1258 (iPad; iOS 13.5; zh-Hans_JP) Cronet",
    sdk_aid: "2292",
    "X-SS-DP": "5245",
    "X-Khronos": "", // timestamp in second
    "X-Gorgon": "8300fe56b000164efdb1a0612f7660f67fd8d852652cc582e7b6", // ??? only last four digits are changing, maybe not verified
  },
  // reserved
  cookies: {
    install_id: "", // = device_id
    ttreq: "", // from device_register
  },
  urls: {
    deviceRegisteration:
      "https://log-sg.bytegsdk.com/service/2/device_register/",
    locationQuery: "https://gsdk-sg.bytegsdk.com/gsdk/misc/location/ipinfo",
    visitorStatus: "https://bsdk19-sg.bytegsdk.com/sdk/account/visitor_status",
    visitorLogin: "https://bsdk19-sg.bytegsdk.com/sdk/account/visitor_login",
    autoLogin: "https://bsdk19-sg.bytegsdk.com/sdk/account/auto_login",
    login: "https://gsdk-sg.bytegsdk.com/gsdk/account/login",
  },
};
export const pjsk = {
  baseURL: {
    jp: "https://production-game-api.sekai.colorfulpalette.org/api",
    tw: "https://mk-zian-obt-https.bytedgame.com/api",
    en: "https://n-production-game-api.sekai-en.com/api",
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
