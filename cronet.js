/**
 * A Cronet emulator, not js client
 */

import axios from "axios";
import log4js from "log4js";
import getTimezoneOffset from "get-timezone-offset";
import { URLSearchParams } from "url";
import { merge } from "./utils";

// const URL_KEY = "__AXIOS-DEBUG-LOG_URL__";

// import isAbsoluteURL from "axios/lib/helpers/isAbsoluteURL";
// import buildURL from "axios/lib/helpers/buildURL";
// import combineURLs from "axios/lib/helpers/combineURLs";
// function getURL(config) {
//   let url = config.url;
//   if (config.baseURL && !isAbsoluteURL(url)) {
//     url = combineURLs(config.baseURL, url);
//   }
//   return buildURL(url, config.params, config.paramsSerializer);
// }

// require("axios-debug-log")({
//   request: function (debug, config) {
//     const url = getURL(config);
//     Object.defineProperty(config, URL_KEY, { value: url });
//     debug(config.method.toUpperCase(), url, config.headers, config.data);
//   },
// });

import { twSDK } from "./constants";

export class CronetClient {
  constructor(options) {
    if (options.logger) {
      this.logger = options.logger;
    } else {
      this.logger = log4js.getLogger("cronet-client");
    }

    const { params, data, urls, headers } = twSDK;
    if (options.params) {
      this.params = merge(params, options.params);
    } else {
      this.params = params;
    }
    if (options.data) {
      this.data = merge(options.data, data);
    } else {
      this.data = data;
    }
    if (options.urls) {
      this.urls = merge(options.urls, urls);
    } else {
      this.urls = urls;
    }
    if (options.headers) {
      this.headers = merge(options.headers, headers);
    } else {
      this.headers = headers;
    }

    if (
      typeof this.params.custom === "string" ||
      this.params.custom instanceof String
    ) {
      this.params.custom = JSON.parse(this.params.custom);
      this.params.intl_info = JSON.parse(this.params.intl_info);
    }
    this.isParamsPrepared = false;

    this.axios = axios.default.create();
  }

  // will call location querying api and change params
  async queryLocation() {
    const location = (await this.axios.get(this.urls.locationQuery)).data.data
      .Location;
    const parsed = {
      countryCode: location.Country.Code,
      countryName: location.Country.ASCIName,
      provinceCode: location.Subdivisions[0].Code,
      provinceName: location.Subdivisions[0].ASCIName,
      cityCode: location.City.Code,
      cityName: location.City.ASCIName,
      timezone: location.Place.TimeZone,
    };
    this.setLocation(parsed);

    return parsed;
  }

  // set location to params
  setLocation(locationInfo) {
    this.params.custom.province = locationInfo.provinceName;
    this.params.custom.city = locationInfo.cityName;
    this.params.custom.country = locationInfo.countryCode.toLowerCase();
    this.params.intl_info.gps_country = locationInfo.countryCode.toLowerCase();
    this.params.intl_info.gps_city = locationInfo.cityName;
    this.params.intl_info.timezone_name = locationInfo.timezone;
    this.params.tz_name = locationInfo.timezone;
    this.params.tz_offset = getTimezoneOffset(
      locationInfo.timezone,
      new Date()
    );
  }

  prepareParams() {
    if (!this.isParamsPrepared) {
      this.params.custom = JSON.stringify(this.params.custom);
      this.params.intl_info = JSON.stringify(this.params.intl_info, null, 2);
      this.isParamsPrepared = true;
    }
    this.params._rticket = new Date().getTime().toString();
  }

  async doVisitorLogin(loginInfo) {
    this.prepareParams();
    const data = merge(this.data.visitorLogin, {});
    for (let key in data) {
      if (this.params[key]) {
        data[key] = this.params[key];
      }
    }
    data.client_uuid = loginInfo.clientUUID;
    data.login_id = loginInfo.loginID;
    this.headers["X-Khronos"] = this.params._rticket.substring(
      0,
      this.params._rticket.length - 3
    );
    const res = (
      await this.axios.post(
        this.urls.visitorLogin,
        new URLSearchParams(data).toString(),
        {
          params: this.params,
          headers: {
            "content-type": "application/x-www-form-urlencoded",
            ...this.headers,
          },
          responseType: "json",
        }
      )
    ).data;

    const { user_id, token } = res.data;

    return {
      userId: String(user_id),
      token,
    };
  }

  async doAutoLogin(loginInfo) {
    this.prepareParams();
    const data = merge(this.data.autoLogin, {});
    for (let key in data) {
      if (this.params[key]) {
        data[key] = this.params[key];
      }
    }
    data.client_uuid = loginInfo.clientUUID;
    data.login_id = loginInfo.loginID;
    data.user_id = loginInfo.userId;
    data.token = loginInfo.token;
    data.access_token = loginInfo.accessToken;

    this.headers["X-Khronos"] = this.params._rticket.substring(
      0,
      this.params._rticket.length - 3
    );
    const res = (
      await this.axios.post(
        this.urls.autoLogin,
        new URLSearchParams(data).toString(),
        {
          params: this.params,
          headers: {
            "content-type": "application/x-www-form-urlencoded",
            ...this.headers,
          },
          responseType: "json",
        }
      )
    ).data;

    const { user_id, token } = res.data;

    return {
      userId: String(user_id),
      token,
    };
  }

  async doLogin(loginInfo) {
    this.prepareParams();
    const data = merge(this.data.login, {});
    if (!loginInfo.accessToken) {
      delete data.access_token;
    } else {
      data.access_token = loginInfo.accessToken;
    }
    for (let key in data) {
      if (this.params[key]) {
        data[key] = this.params[key];
      }
    }
    data.client_uuid = loginInfo.clientUUID;
    data.login_id = loginInfo.loginID;
    data.data.user_id = loginInfo.userId;
    data.data.token = loginInfo.token;
    data.data = JSON.stringify(data.data, null, 2);

    this.headers["X-Khronos"] = this.params._rticket.substring(
      0,
      this.params._rticket.length - 3
    );
    const res = (
      await this.axios.post(
        this.urls.login,
        new URLSearchParams(data).toString(),
        {
          params: this.params,
          headers: {
            "content-type": "application/x-www-form-urlencoded",
            ...this.headers,
          },
          responseType: "json",
        }
      )
    ).data;

    const { access_token, sdk_open_id } = res.data;

    return {
      accessToken: access_token,
      sdkOpenId: sdk_open_id,
    };
  }
}
