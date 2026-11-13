# Open Your Eyes（Claude 插件）

把闲置手机当作 Claude 的眼睛。Claude 想看实物（电路板、接线、屏幕、零件）时调用 `look`，
你在电脑上点「允许」，工作台上的手机（可以多台）立即拍照，照片直接交回这个对话。

## 使用前

1. 电脑上安装并打开 Open Your Eyes 程序（菜单栏 / 任务栏会出现图标，并打开设置页 http://127.0.0.1:39757/settings）
2. 手机扫设置页「＋ 添加机位」的二维码，按提示信任一次证书，打开拍照页并保持在前台

手机用浏览器就行（安卓也可以装可选的 App）；照片只存在你的电脑上，不经过任何服务器。

最省事的装法：在 Open Your Eyes 窗口的「连接 AI」里点 Claude Code 的「一键安装」。

## 工具

| 工具 | 作用 |
|---|---|
| `look(what, phone?, light?)` | 拍照：默认所有在线手机同时拍，可指定机位和补光 |
| `look_clip(what, seconds, frames, phone?, light?)` | 一段时间内连拍几帧 |
| `zoom(x, y, w, h, phone?)` | 放大本对话最近一次照片的局部（从原图裁剪） |
| `eyes_status()` | 各机位是否在线、有没有补光灯 |

插件本身不含程序：它连接电脑上 Open Your Eyes 程序提供的本机接口（`http://127.0.0.1:39757/mcp`），
不需要 Python、Node 或任何其它运行环境。只接受本机的请求。

Claude 桌面版请改用扩展包 `open-your-eyes.mcpb`（双击安装）。
