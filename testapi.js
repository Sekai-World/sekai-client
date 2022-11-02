import { decrypt, encrypt, APIClient } from "./apiclient";

// const apiClient = new APIClient(null, "en");

// apiClient.account = {
//   userId: "162113639289176072",
//   credential:
//     "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJjcmVkZW50aWFsIjoiOWQ1NmMxZTAtMjY1Zi00NjliLWJlODUtZWMwNjUzNmI0ZTlkIiwidXNlcklkIjoiMTYyMTEzNjM5Mjg5MTc2MDcyIn0.V60HNXYnSD2HiPeHjh_vXEL3kn0nSAWpED6Zl-bTK58",
// };

// (async () => {
// console.log(await apiClient.callAPI("/system"));
// await apiClient.checkVersions();
// console.log(await apiClient.registerAccount());
// await apiClient.login();
// console.log(await apiClient.callAPI("/cheerful-carnival-team-point/39"));
// console.log(await apiClient.callAPI("/cheerful-carnival-team-count/39"));
// })();

import jayson from "jayson/promise";
// import { writeFileSync } from "fs";

const client = new jayson.Client.http({
  port: process.env.SERVER_PORT || 39390,
});

(async () => {
  console.log(await client.request("init", ["jp"]));
  // console.log(await client.request("callAPI", ["/system"]));
  // console.log((await client.request("callAPI", ["/suite/master"])).result);
  console.log(await client.request("checkVersions", []));
  // console.log(await client.request("login", []));
  // console.log(await client.request("account", []));
  // console.log(await client.request("versionInfo", []));
  // const { userId } = (await client.request("account", [])).result;
  // const userId = 6330496837763082;
  // console.log(await client.request("callAPI", [`/user/${userId}/profile`]));
  // writeFileSync(
  //   `${userId}.json`,
  //   JSON.stringify(
  //     (await client.request("callAPI", [`/user/${userId}/profile`])).result
  //   ),
  //   {
  //     encoding: "utf8",
  //   }
  // );
})();
