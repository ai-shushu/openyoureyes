/* Open Your Eyes 拍照页离线缓存
 *
 * 目的：首次通过 USB + adb reverse 以 http://localhost 打开后，
 * 即使拔掉数据线、localhost 不再可连，刷新页面也能从缓存重新加载，
 * 且 origin 仍是 localhost（安全上下文），getUserMedia 依旧可用。
 *
 * 策略：网络优先（在线时永远拿最新代码），失败才回缓存。
 */
const CACHE = "openyoureyes-v7";   // 改版本号会清掉旧缓存（v3 曾误缓存过接口回包）
const ASSETS = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icon.svg",
  "/config.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      ),
      self.clients.claim(),
    ])
  );
});

// 网络优先：在线时永远用最新页面；网络 8 秒都没回来（基本等于断网）才用缓存。
// 即使这次先用了缓存，网络回来后照样更新缓存——之前 1.5 秒就放弃且不更新缓存，
// 慢的手机（实测某台安卓）就一直卡在旧页面上，怎么刷新都是旧的。
const NET_WAIT_MS = 8000;

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // 跨域的 WHIP/WHEP 交给浏览器
  // 接口请求（拍照任务轮询、上传等）一律直连电脑，绝不走缓存：
  // 轮询会在电脑上挂几秒，若被这里超时后换成缓存里的旧回包，就会丢任务或把旧任务反复重拍
  if (url.pathname.startsWith("/api/")) return;

  const net = fetch(req, { cache: "no-store" }).then((res) => {
    if (res && res.status === 200) {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
    }
    return res;
  });
  event.waitUntil(net.catch(() => {}));   // 让缓存更新在页面已显示后也能完成
  event.respondWith(
    Promise.race([net, new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), NET_WAIT_MS))])
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/index.html")).then((hit) => hit || net))
  );
});
