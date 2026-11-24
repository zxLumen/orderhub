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
| `login_required` | 该平台登录态失效 | 网页平台：agent 会自动切可见 Chrome 打开登录页并每 5 分钟重试；抖音：agent 会自动启动 MuMu 并打开抖音 App，在模拟器里登录后自动抓取会话（不弹浏览器） |
| `captcha` | 撞到滑块/验证码 | 人工过掉，再点采集（不做自动绕过） |
| `invalid_page` | 订单页 URL 变了 | 在仓库 `.env` 设 `ORDERHUB_URL_TAOBAO=<登录后订单页地址>`，重跑 `update_agent.bat` |
| `ok · 0 条` | 页面结构变了，解析没命中 | 把 `data/debug/<平台>-empty-*.html/.png` 回传，改 `app/collectors/web_parser.py` |

## 抖音 / 转转

抖音不再走网页 / OCR。App 的订单列表背后是电商 JSON 接口 `ec.snssdk.com/order/page`，只需要登录 Cookie + App 参数，不需要 OCR、Frida 或逐个点订单。

登录态只从已 root 的 MuMu 模拟器 WebView（`app_webview/Default/Cookies`，明文）读取。抖音订单接口只认 App 会话，Chrome 网页登录会被拒绝（`10001010A`），所以**不要**用网页登录刷新抖音。

必须带全 `.snssdk.com` 的 Cookie，尤其是 `session_tlb_tag` 和 `multi_sids`；只取 `sessionid` 等少数几个会被接口判定为未登录。

agent 每天自动采集一次。采集时抖音 App 保持关闭或后台即可，不需要点击订单页。

### 登录态过期

会话 `sid_guard` 约 60 天，`session_tlb_tag` 可能更短。过期时 agent 不会弹浏览器，而是：

1. 自动启动 MuMu（没开就开），等待 ADB 就绪
2. 自动打开抖音 App，把模拟器窗口带到前台
3. 你在模拟器里确认登录（已登录会自动继续；掉登录就扫码一次），登录后留在首页即可，不要打开订单页
4. agent 每 30 秒只读检查一次 App 的 WebView 会话
5. 发现新的 `sessionid` 后验证接口，通过就保存完整 Cookie 集
6. 自动重跑采集，恢复

如果 15 分钟内没登录成功，本次放弃，等下一次每日调度再试；不会反复弹窗、不会 force-stop、不会点订单页。

也可以手动刷新（模拟器里登录后完全退出抖音，再运行）：

```powershell
cd E:\Project\orderhub
.\.venv\Scripts\python.exe -m app.collectors.douyin --refresh
```

手动验证：

```powershell
cd E:\Project\orderhub
.\.venv\Scripts\python.exe -m app.collectors.douyin
```

Cookie 文件在 `C:\OrderHub\data\douyin-cookies.json`，最后一次验证通过的备份是同目录的 `douyin-cookies.good.json`。只有接口验证通过才会覆盖。

转转同样没有稳定的公开订单页，没有可用 URL 时走导入。如果你确实找到了可用的订单页地址，可以覆盖：

```ini
ORDERHUB_URL_ZHUANZHUAN=<登录后订单页地址>
```

改完 `.env` 必须重跑 `update_agent.bat` 让 agent 重新加载。

## 备注

- 网页平台用你自己 Chrome 的登录态；抖音只从模拟器 App 的 WebView 读取。服务端不保存平台 cookie。
- 每个平台采完会把结果写进 `collector_runs`，历史记录在首页「采集记录」里可查。
- 调试文件在 `data/debug/`，只有失败或 0 条时才会生成。
