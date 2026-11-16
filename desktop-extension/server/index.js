#!/usr/bin/env node
// Open Your Eyes 的 Claude 桌面版扩展：把 Claude 的 stdio 消息转给本机的 Open Your Eyes 程序（http://127.0.0.1:39757/mcp）。
// 用 Claude 桌面版自带的 Node 运行，不需要用户装任何东西；没有第三方依赖。
// 用 Node 自带的 http 模块直连本机，不用 fetch：fetch 在某些环境下会跟随代理设置（代理软件开着就连不上），还有 5 分钟的等待上限。
// Open Your Eyes 没开时：工具照常列出，调用时用大白话告诉用户打开 Open Your Eyes。
"use strict";
const path = require("path");
const fs = require("fs");
const readline = require("readline");
const http = require("http");

const URL_ = process.env.T9_EYES_MCP || "http://127.0.0.1:39757/mcp";
const TOOLS = JSON.parse(fs.readFileSync(path.join(__dirname, "tools.json"), "utf8"));
const INSTRUCTIONS = (() => { try { return fs.readFileSync(path.join(__dirname, "instructions.txt"), "utf8"); } catch (e) { return ""; } })();
const OFFLINE = "Open Your Eyes 没有运行：请让用户在电脑上打开 Open Your Eyes 程序（打开后会出现在菜单栏 / 任务栏），再试一次。";
const FAILED = "Open Your Eyes 开着，但这次没有回应（%s）：请再试一次；还不行就让用户退出并重新打开 Open Your Eyes。";
let sid = null;
let initParams = null;

// 直连本机发一条 POST（不走任何代理）。返回 {status, headers, body}。
function rawPost(url, headers, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const data = Buffer.from(body, "utf8");
    const req = http.request({ host: u.hostname, port: u.port || 80, path: u.pathname + u.search, method: "POST",
      agent: false, headers: Object.assign({ "Content-Length": data.length }, headers) }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString("utf8") }));
      res.on("error", (e) => { e.sent = true; reject(e); });
    });
    req.setTimeout(330000, () => { const e = new Error("等了太久"); e.sent = true; req.destroy(e); });   // 拍照要等用户取景，最长 5 分钟
    req.on("error", reject);
    req.end(data);
  });
}

function out(msg) { process.stdout.write(JSON.stringify(msg) + "\n"); }

async function post(msg) {
  const headers = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
  if (sid) headers["Mcp-Session-Id"] = sid;
  const r = await rawPost(URL_, headers, JSON.stringify(msg));
  const s = r.headers["mcp-session-id"];
  if (s) sid = s;
  if (r.status < 200 || r.status >= 300) { const e = new Error("HTTP " + r.status); e.sent = true; throw e; }
  if (r.status === 202 || !r.body.trim()) return null;
  return JSON.parse(r.body);
}

async function ensureSession() {
  if (sid || !initParams) return;
  await post({ jsonrpc: "2.0", id: "bridge-init", method: "initialize", params: initParams });
  await post({ jsonrpc: "2.0", method: "notifications/initialized" });
}

async function handle(msg) {
  const id = msg.id, method = msg.method;
  if (method === "initialize") {
    initParams = msg.params || {};
    try { const r = await post(msg); if (r) return out(r); } catch (e) { /* Open Your Eyes 没开：先本地应答 */ }
    return out({ jsonrpc: "2.0", id, result: {
      protocolVersion: (msg.params && msg.params.protocolVersion) || "2025-06-18",
      capabilities: { tools: {}, prompts: {} }, instructions: INSTRUCTIONS, serverInfo: { name: "open-your-eyes", version: "bridge" } } });
  }
  if (id === undefined || id === null) {   // 通知
    try { await post(msg); } catch (e) {}
    return;
  }
  try {
    await ensureSession();
    const r = await post(msg);
    if (r) return out(r);
  } catch (e) {
    sid = null;   // 下次重新建立会话
    if (method === "tools/list") return out({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
    if (method === "ping") return out({ jsonrpc: "2.0", id, result: {} });
    // 连不上 = 程序没开；连上了但出错 / 等太久 = 程序开着，只是这次没成（不自动重发：拍照请求重发会多拍一次）
    const text = e && e.sent ? FAILED.replace("%s", e.message) : OFFLINE;
    if (method === "tools/call") return out({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text }], isError: true } });
    return out({ jsonrpc: "2.0", id, error: { code: -32603, message: text } });
  }
}

const rl = readline.createInterface({ input: process.stdin });
let chain = Promise.resolve();
rl.on("line", (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try { msg = JSON.parse(line); } catch (e) { return; }
  // 工具调用可能要等几十秒（等手机拍照），其余消息不必排队等它
  if (msg.method === "tools/call") { handle(msg); return; }
  chain = chain.then(() => handle(msg));
});
