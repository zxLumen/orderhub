# OrderHub 状态

- 版本：0.4.0（2026-11-11）
- 线上：https://orderhub.zxlumen.cn（systemd `orderhub`，`/opt/orderhub`，数据 `/var/lib/orderhub/orderhub.db`）
- 测试：109 pass / 0 fail
- 当前生产数据：1386 单（全部京东）、39 个包裹、201 条轨迹

## 本版新增

- 平台注册表：京东 / 淘宝天猫 / 拼多多 / 抖音 / 闲鱼 / 转转 / 美团 / 饿了么 / 唯品会 / 小红书 / 其他
- Windows agent 五平台采集：淘宝、拼多多、闲鱼（登录页已实测识别）；抖音、转转为实验性首页导航 + URL 覆盖
- 采集器嗅探页面 JSON + 文本兜底，只写解析到订单号的行；状态 ok/empty/login/captcha/invalid
- 登录失效时 agent 切可见 Chrome 并打开登录页，5 分钟自动重试
- 导入：支付宝 ZIP/CSV、微信 CSV、平台个人信息副本 CSV/XLSX/JSON/ZIP，幂等
- UI：平台筛选、平台覆盖面板（含「采集」按钮）、导入面板、补物流、批量短信
- 物流：无付费 key 时走快递100 公开查询（手动补单，默认不自动轮询）
- 数据：`orders.source` 记录来源（agent/alipay/wechat/export/manual），旧库自动迁移

## 待办 / 已知边界

- 需要真实导出文件校对列名：淘宝、拼多多、抖音、闲鱼、转转各一份「个人信息副本」
- 账单通常无法判断平台，会落到「其他」或按商户关键词猜；平台副本优先
- 采集器使用你自己 Chrome 的登录态，不做验证码绕过；服务端不保存平台 cookie
- 抖音/转转没有稳定公开订单页（实测 404/仅下载 App），需 `ORDERHUB_URL_*` 覆盖或走导入
- 快递100 公开接口限流严重（实测同一单号时好时坏，还出现过 2023 年旧轨迹）；已加 30 天时间校验并默认只做手动查询，要稳定自动追踪建议配付费 key
- 支付宝/微信账单不提供个人 API，只能人工下载后导入

## 回滚

- 代码：`git revert <commit>` 或 `/opt/orderhub` 切回上一 tag
- 数据库：升级前备份；`source` 列新增，不破坏旧字段
