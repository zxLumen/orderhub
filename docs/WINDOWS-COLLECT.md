# Windows 五平台采集 · 10 分钟验收

目标：把这台 Windows 的 agent 从 v0.1.0 升到 v0.4.0，采到淘宝 / 拼多多 / 闲鱼的真实订单。
抖音网页版没有消费者订单页（实测），只能用个人信息副本导入；转转的网页入口同样不稳定，先跳过。

## 1. 升级 agent（约 1 分钟）

双击 `scripts\update_agent.bat`，等它跑完。

期望最后一行是版本号：

```
0.4.0
```

失败对照：

| 输出 | 原因 | 处理 |
|---|---|---|
| `Update failed` + `git pull` 报错 | 本地有改动 / 分支不对 / SSH 连不上 GitHub | 在仓库目录跑 `git status`，`git pull --ff-only` 单独重试 |
| `pip install` 超时 | 网络到 pypi.org 不通 | 重跑一次；或换镜像装 `requirements-jd.txt` |
| 打印仍是 `0.1.0` 或没有版本行 | 旧进程没被替换 | 任务管理器杀掉 `python.exe`（命令行含 `app.collector_agent`），再跑一次 |

## 2. 在 9333 那个 Chrome 里登录三平台（约 5 分钟）

用 `scripts\start_chrome.bat` 启动的 Chrome（调试端口 9333），逐个打开并登录：

- 淘宝：https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm
- 拼多多：https://mobile.yangkeduo.com/orders.html
- 闲鱼：https://www.goofish.com/bought

要求：登录后能直接看到「全部订单 / 订单号」列表，不要停在登录页或验证码页。

## 3. 点采集（约 2 分钟）

打开 https://orderhub.zxlumen.cn → 更多 → 平台覆盖 → 淘宝 / 拼多多 / 闲鱼 各点一次「采集」，确认弹窗。

期望（8 秒后自动刷新）：

- 采集记录出现 `<平台> ok · N 条`
- 采集器状态显示 `在线 · 0.4.0`
- 订单页按平台筛选能看到新订单

## 失败对照

| 采集记录 | 含义 | 处理 |
|---|---|---|
| 409 `五平台采集需要 v0.4.0` | 服务端看到的 agent 还是旧版 | 回步骤 1，确认脚本最后打印 `0.4.0` |
| `login_required` | 该平台登录态失效 | agent 会自动切可见 Chrome 打开登录页并每 5 分钟重试；登录完再点一次采集 |
| `captcha` | 撞到滑块/验证码 | 人工过掉，再点采集（不做自动绕过） |
| `invalid_page` | 订单页 URL 变了 | 在仓库 `.env` 设 `ORDERHUB_URL_TAOBAO=<登录后订单页地址>`，重跑 `update_agent.bat` |
| `ok · 0 条` | 页面结构变了，解析没命中 | 把 `data/debug/<平台>-empty-*.html/.png` 回传，改 `app/collectors/web_parser.py` |

## 抖音 / 转转

抖音不再走网页 / OCR。App 的订单列表背后是电商 JSON 接口 `ec.snssdk.com/order/page`，只需要登录 Cookie + App 参数，不需要 OCR、Frida 或逐个点订单。

登录态来源按优先级自动选择：

1. 已登录的 Chrome 9333（在 Chrome 里打开 `https://www.douyin.com/` 扫码登录一次即可，不需要模拟器）
2. 已 root 的 MuMu 模拟器 WebView（`app_webview/Default/Cookies`，明文）

agent 每 6 小时自动拉一次。Cookie 失效时会重新按上面的顺序刷新；模拟器不是必须常驻。

只用 Chrome 刷新的命令：

```powershell
.\.venv\Scripts\python.exe -m app.collectors.douyin --refresh --source chrome
```

手动验证：

```powershell
cd E:\Project\orderhub
.\.venv\Scripts\python.exe -m app.collectors.douyin
```

转转同样没有稳定的公开订单页，没有可用 URL 时走导入。如果你确实找到了可用的订单页地址，可以覆盖：

```ini
ORDERHUB_URL_ZHUANZHUAN=<登录后订单页地址>
```

改完 `.env` 必须重跑 `update_agent.bat` 让 agent 重新加载。

## 备注

- 采集器用你自己 Chrome 的登录态，服务端不保存平台 cookie。
- 每个平台采完会把结果写进 `collector_runs`，历史记录在首页「采集记录」里可查。
- 调试文件在 `data/debug/`，只有失败或 0 条时才会生成。
