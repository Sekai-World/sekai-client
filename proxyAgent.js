const got = require("got").default;
const PQueue = require("p-queue").default;
const { baseURL } = require("./constants");
const { HttpsProxyAgent } = require("hpagent");

const queue = new PQueue({ concurrency: 15 });

async function getProxyList() {
  const rawList = await got("https://www.89ip.cn/tqdl.html?api=1&num=9999");
  const matchList = rawList.body.match(
    RegExp("\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\:\\d{1,5}", "g")
  );

  return matchList;
}

async function selectProxy() {
  const proxyList = await getProxyList();
  // const workers = [];

  proxyList.forEach((proxy) =>
    queue.add(() =>
      got(baseURL, {
        agent: {
          https: new HttpsProxyAgent({
            proxy: `http://${proxy}`,
          }),
        },
        timeout: 5000,
      })
        .then(() => console.log(`${proxy} is available!`))
        // .catch(() => console.log(`unable to connect ${proxy}!`))
        .catch(() => undefined)
    )
  );
}

require.main === module && selectProxy();
