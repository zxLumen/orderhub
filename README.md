# OrderHub

跨平台订单 / 包裹 / 物流汇总，自用、本地优先。

第一版目标很明确：**不登四个平台也能看到东西到哪了；搜得到过去买过什么；需要更全时再加京东采集。**

- 后端：Python 标准库（无需 pip 安装）
- 前端：原生 PWA，iPhone 添加到主屏幕即可
- 数据：SQLite + 中文二元分词全文索引
- 推送：Bark
- 物流：Kuaidi100（可选）
- 采集：京东（可选，Playwright + 已登录的真实 Chrome）

---

## 已实现

- 短信接入：`POST /api/sms`，解析运单号、承运商、取件码、驿站、状态
- 包裹看板：在途 / 待取件 / 已签收，物流轨迹时间线
- 订单看板：最近订单、金额、状态、运单号
- 全文搜索：商品名、店铺、运单号、短信原文
- 抖音抓屏入库：`POST /api/captures`
- 手动补单：PWA → 更多 → 手动添加（美团外卖、抖音、任何漏掉的单）
- 京东采集：CDP 接管真实 Chrome，记录验证码次数
- Bark 推送：待取件、派送中、签收、外卖状态
- 自动对账基础：订单、包裹、短信统一存库
- 导出：`/api/export`，JSON 备份
- CSV/JSON 订单导入：`scripts/import_orders.py`

## 还没做（下一步）

- 支付宝 / 微信账单 CSV 自动导入
- 四家“个人信息副本”专用导入适配器
- 淘宝 / 美团 Chrome 采集
- 退货 / 价保倒计时
- 商品图片缓存

---

## 快速开始（Windows）

### 1. 安装

- Python 3.11+（3.12 推荐）
- Google Chrome
- （可选）Tailscale：让 iPhone 在外网也能访问家里的 Windows
- （可选）Bark：iPhone 推送

核心不需要任何第三方 Python 包。只有京东采集需要 Playwright：

```bat
cd C:\OrderHub
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements-jd.txt
```

### 2. 配置

```bat
copy .env.example .env
notepad .env
```

至少改这三项：

```ini
ORDERHUB_TOKEN=一串很长的随机字符串
ORDERHUB_DATA_DIR=C:\OrderHub\data
BARK_URL=https://api.day.app/你的key
```

默认只监听 `127.0.0.1`，Tailscale Serve 会代理它。只有想用局域网 IP 直连时才改成：

```ini
ORDERHUB_HOST=0.0.0.0
```

并且必须同时设置 token；否则服务会拒绝启动。

生成令牌：

```bat
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. 启动

```bat
scripts\start_orderhub.bat
```

或：

```bat
python -m app.server
```

打开 <http://127.0.0.1:8000>。

### 4. iPhone 访问

1. Windows 和 iPhone 都装 Tailscale，登录同一账号
2. 在 Windows 上开一个 HTTPS 入口（PWA / Service Worker 需要安全上下文）：

```bat
tailscale serve --bg 8000
:: 如果上面的写法不识别，用：
:: tailscale serve --bg http://127.0.0.1:8000
```

它会给出一个 `https://<机器名>.<tailnet>.ts.net/` 地址。
3. iPhone Safari 打开这个 HTTPS 地址
4. 首次输入 `ORDERHUB_TOKEN`
5. 分享 → 添加到主屏幕

> 如果只用 `http://<Tailscale IP>:8000`，页面能用，但 Service Worker / 离线缓存不会生效。
> 不要把 8000 端口映射到公网。Tailscale 足够，且更安全。

---

### 开机自启（可选）

仓库自带幂等守护，不需要 NSSM：

- `scripts\ensure_running.ps1`：9333 不通就起无头 Chrome，8000 不通就起服务，两个都通什么都不做
- `scripts\autostart.vbs`：以零窗口方式调用上面的脚本
- 注册任务计划（当前用户即可，无需管理员）：

```powershell
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument '"<仓库路径>\scripts\autostart.vbs"'
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "OrderHub" -Action $action -Trigger @($t1, $t2) `
        -Settings $settings -Principal (New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive) -Force
```

> 登录时跑一次，之后每 5 分钟自检，服务或 Chrome 挂了会被拉起。
> 日志在 `data\autostart.log` 和 `data\chrome.log`。

## iPhone 短信自动化（iOS 17+）

目标：收到快递 / 驿站 / 外卖短信时，自动 POST 到 OrderHub。

### 创建自动化

1. 快捷指令 → 自动化 → 新建
2. 触发：**信息** → 信息包含，例如：
   - `快递`
   - `包裹`
   - `驿站`
   - `丰巢`
   - `取件`
   - `京东`
   - `美团`
   - `抖音`
   - `发货`
   - `送达`
3. 选择 **立即运行**
4. 动作：
   - `获取快捷指令输入`（或直接使用“信息”变量）
   - `获取 URL 内容`
     - URL：`https://<机器名>.<tailnet>.ts.net/api/sms`（或局域网内的 `http://<IP>:8000/api/sms`）
     - 方法：POST
     - 头部：`Authorization: Bearer <ORDERHUB_TOKEN>`
     - 请求体：JSON
       - `text`：短信内容
       - `received_at`：当前日期
       - `sender`：发送者（可选）

### 必须排除验证码

在 POST 之前加一个“如果”：

- 如果短信内容 **包含** `验证码` 或 `校验码` 或 `动态码` → 停止
- 否则才发送

只转发物流相关短信，不要把银行、登录验证码送到服务器。

### 测试

在 Windows 上：

```bat
curl -X POST http://127.0.0.1:8000/api/sms ^
  -H "Authorization: Bearer 你的token" ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"【顺丰速运】您的快件 SF1234567890123 已存放至阳光花园菜鸟驿站，取件码：8-2-1234。\"}"
```

然后刷新 PWA，包裹看板里应该出现这一单。

---

## Bark 推送

1. iPhone 安装 Bark
2. 复制 App 里的 URL，例如 `https://api.day.app/xxxxxxxx`
3. 写入 `.env`：

```ini
BARK_URL=https://api.day.app/xxxxxxxx
```

不配置也能用，只是不推送。

---

## 快递100（可选）

1. 注册 <https://api.kuaidi100.com/>
2. 拿到 `customer` 和 `key`
3. 写入 `.env`：

```ini
KUAIDI100_CUSTOMER=你的customer
KUAIDI100_KEY=你的key
# 可选：部分加密运单需要收件人手机号后四位
KUAIDI100_PHONE=1234
```

作用：

- 短信拿到运单号后，自动补全物流轨迹
- 在途包裹每 30 分钟刷新
- 状态变化推送 Bark

没有配置时，包裹只显示短信里的状态，不影响其他功能。

---

## 京东采集（可选）

### 为什么用 CDP

不自己启动 Playwright 自带的浏览器，而是接管一个真实 Chrome：真实 Cookie、真实 IP、真实指纹，验证码频率最低。

### 步骤

1. 首次登录用可见窗口：双击 `scripts\jd_login.bat`，在弹出的 Chrome 里扫码登录京东
2. 登录后切回无窗口常驻：双击 `scripts\jd_background.bat`
   - 后台模式用 `--headless=new`，没有窗口、没有任务栏图标、不占 Alt+Tab
   - 同时覆盖 UA，避免 `HeadlessChrome` 特征暴露
   - 两种模式共用 `data\chrome-profile`，Cookie 不会丢
3. `.env` 打开采集：

```ini
ENABLE_JD_COLLECTOR=1
JD_CDP_URL=http://127.0.0.1:9333
JD_INTERVAL_SECONDS=21600
JD_MAX_PAGES=50
JD_MAX_SCROLLS=8
```

4. 重启 OrderHub
5. PWA → 更多：
   - 「采集京东」只采近 3 个月（定时任务也走这个模式）
   - 「全量采集」遍历「今年内 / 各年 / 2014 前」所有档位，用来一次性补历史

### 采集内容

- 订单列表接口（`pcorder_center_list`）返回的**整条 JSON 会存进 `orders.raw_json`**：商品、数量、价格与支付方式、店铺、状态、详情页 PassKey、收货信息、按钮配置等
- 常用字段由它派生到列上：`url`（详情页）、`product_url`（商品页）、`image_url`（商品图）、`logistics_text`（最近物流文案）
- 以后想加字段（支付方式、商品数量、子订单、标签…）**只改读取逻辑，不需要重新采集**
- 全量采集只跑一次用于补历史；日常定时任务只采近 3 个月

### 验证码 / 登录失效

- **重启电脑不需要重新登录**：登录态存在 profile 的 Cookie 里，通常几周到几个月才失效
- 采集记录出现 `login_required` 或 `captcha` 时：`scripts\jd_login.bat` → 完成登录/滑块 → `scripts\jd_background.bat` 切回后台
- 每次运行都会记录在「采集记录」里，方便统计验证码频率

### 安全

- `9333` 只监听本机，**不要做端口映射**
- 采集器只读订单，不下单、不支付

---

## 抖音

抖音没有消费者网页订单页，所以不走 Chrome：

- 物流：短信 + 快递100
- 商品名：季度“个人信息副本”导出，用 `import_orders.py` 导入
- 运单号缺失：用快捷指令抓屏，POST 到 `/api/captures`
- 不需要安卓、不需要模拟器

抓屏快捷指令建议绑定“轻点背面”：

1. 截屏
2. 从图像中提取文字
3. POST `https://<机器名>.<tailnet>.ts.net/api/captures`
   - JSON：`platform` = `douyin`，`text` = 识别文字，`captured_at` = 当前日期
4. 设置 → 辅助功能 → 触控 → 轻点背面 → 双击 → 选择该快捷指令

---

## 导入历史订单

CSV 示例：

```csv
platform,order_id,title,shop,amount,status,status_text,order_time,tracking_no
taobao,123456,绿联充电线,绿联旗舰店,29.90,shipped,运输中,2026-10-20 09:12:00,YT9876543210123
douyin,987654,无线蓝牙耳机,某旗舰店,199.00,completed,已完成,2026-10-01 12:00:00,
```

导入：

```bat
python scripts\import_orders.py orders.csv --platform taobao
```

也支持 JSON 数组，或 `{ "orders": [...] }`。表头支持常见中英文别名；不认识的列会保留在 `raw_json`。

---

## 数据与备份

- 数据库：`data/orderhub.db`
- 调试 HTML/截图：`data/debug/`
- Chrome 登录态：`data/chrome-profile/`
- 导出：PWA → 更多 → 导出数据，或 `GET /api/export`

`chrome-profile` 里是登录 Cookie，**备份时必须加密**。

---

## API 速查

所有 `/api/*`（除 `/api/health`、`/api/config`）都需要：

```
Authorization: Bearer <ORDERHUB_TOKEN>
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 公开配置（不含密钥） |
| GET | `/api/orders` | 订单列表 |
| POST | `/api/orders` | 新增/更新订单 |
| POST | `/api/orders/import` | 批量导入 |
| GET | `/api/shipments` | 包裹列表 |
| GET | `/api/shipments/{no}` | 包裹详情 + 轨迹 |
| POST | `/api/sms` | 短信接入 |
| GET | `/api/sms` | 短信列表 |
| POST | `/api/captures` | 抖音抓屏 |
| GET | `/api/captures` | 抓屏列表 |
| GET | `/api/search?q=` | 全文搜索 |
| GET | `/api/stats` | 概览 + 采集记录 |
| POST | `/api/parcels/poll` | 手动刷新包裹 |
| POST | `/api/collectors/jd/run` | 手动触发京东采集 |
| GET | `/api/export` | 全量 JSON 导出 |

---

## 测试

```bat
python -m unittest discover -v
```

覆盖：短信解析（平台/运单号/状态）、去重、包裹状态按事件时间推进、退款只改订单、中文搜索、路径解析、JSON 导入、Bark payload、HTTP 鉴权/路径穿越/body 上限、京东 HTML/时间解析。

---

## 限制

- 美团骑手实时地图只在美团 App，第三方拿不到；OrderHub 只能做状态和提醒
- 抖音没有网页订单页；商品名依赖季度副本或抓屏
- 京东页面改版会导致解析失败，需要更新 `app/collectors/jd_parser.py`
- 京东验证码频率取决于账号、设备、IP、频率；用“采集记录”观察，太频繁就关掉
- 不上架 App Store；个人自用
- 自动化操作平台页面可能违反平台条款，风险自担；只读、低频、不绕过验证码

---

## 目录

```
orderhub/
├── app/
│   ├── api.py              # HTTP API + PWA 静态服务
│   ├── db.py               # SQLite + FTS5
│   ├── config.py
│   ├── server.py
│   ├── scheduler.py
│   ├── parsers/sms.py
│   ├── services/
│   │   ├── ingest.py
│   │   ├── kuaidi100.py
│   │   └── bark.py
│   └── collectors/
│       ├── jd.py
│       ├── jd_parser.py
│       └── html_dom.py
├── web/                    # PWA
├── scripts/                # 启动、导入、图标
├── tests/
└── data/                   # 运行时数据（不入库）
```
