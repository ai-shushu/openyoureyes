#!/usr/bin/env node
// Open Your Eyes 的 Claude 桌面版扩展：把 Claude 的 stdio 消息转给本机的 Open Your Eyes 程序（http://127.0.0.1:39757/mcp）。
// 用 Claude 桌面版自带的 Node 运行，不需要用户装任何东西；没有第三方依赖。
// Open Your Eyes 没开时：工具照常列出，调用时用大白话告诉用户打开 Open Your Eyes。
"use strict";
const path = require("path");
const fs = require("fs");
const readline = require("readline");

const URLS = process.env.T9_EYES_MCP ? [process.env.T9_EYES_MCP]
  : ["http://127.0.0.1:39757/mcp", "http://127.0.0.1:8080/mcp"];   // 新端口优先；早期版本用 8080
let URL_ = URLS[0];
const TOOLS = JSON.parse(fs.readFileSync(path.join(__dirname, "tools.json"), "utf8"));
const INSTRUCTIONS = (() => { try { return fs.readFileSync(path.join(__dirname, "instructions.txt"), "utf8"); } catch (e) { return ""; } })();
const OFFLINE = "Open Your Eyes 没有运行：请让用户在电脑上打开 Open Your Eyes 程序（打开后会出现在菜单栏 / 任务栏），再试一次。";
let sid = null;
let initParams = null;

function out(msg) { process.stdout.write(JSON.stringify(msg) + "\n"); }

async function post(msg) {
  const headers = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
  if (sid) headers["Mcp-Session-Id"] = sid;
  let r;
  try {
    r = await fetch(URL_, { method: "POST", headers, body: JSON.stringify(msg) });
  } catch (e) {   // 当前地址连不上：试另一个端口
    const other = URLS.find((u) => u !== URL_);
    if (!other) throw e;
    r = await fetch(other, { method: "POST", headers, body: JSON.stringify(msg) });
    URL_ = other;
  }
  const s = r.headers.get("mcp-session-id");
  if (s) sid = s;
  if (r.status === 202) return null;
  if (!r.ok) throw new Error("HTTP " + r.status);
  return await r.json();
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
    if (method === "tools/call") return out({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: OFFLINE }], isError: true } });
    return out({ jsonrpc: "2.0", id, error: { code: -32603, message: OFFLINE } });
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
