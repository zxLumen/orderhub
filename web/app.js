/* OrderHub PWA — no build step, no framework. */
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const PLATFORM_LABELS = {
    jd: "京东", taobao: "淘宝", douyin: "抖音", meituan: "美团",
    pdd: "拼多多", vip: "唯品会", eleme: "饿了么", other: "其他", unknown: "其他",
  };
  const STATUS_LABELS = {
    unknown: "未知", pending_payment: "待付款", processing: "待发货",
    shipped: "已发货", completed: "已完成", cancelled: "已取消",
    refunded: "已退款", after_sales: "售后", accepted: "已接单",
    in_transit: "运输中", out_for_delivery: "派送中", ready_for_pickup: "待取件",
    delivering: "配送中", delivered: "已签收", returned: "已退回",
  };
  const STATUS_TONE = {
    pending_payment: "amber", processing: "amber", shipped: "blue",
    completed: "green", cancelled: "red", refunded: "red", after_sales: "amber",
    accepted: "blue", in_transit: "blue", out_for_delivery: "amber",
    ready_for_pickup: "green", delivering: "amber", delivered: "green",
    returned: "red", unknown: "gray",
  };

  const state = {
    token: localStorage.getItem("orderhub_token") || "",
    tab: "recent",
    config: null,
    stats: null,
    orders: [],
    shipments: [],
    sms: [],
    captures: [],
    alerts: [],
    searchResults: [],
    searchQuery: "",
    platform: "",
    parcelActive: true,
    loading: false,
  };

  // ------------------------------------------------------------------
  // Utilities
  // ------------------------------------------------------------------
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function money(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "";
    return "¥" + number.toFixed(number % 1 === 0 ? 0 : 2);
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function timeText(value) {
    const date = parseDate(value);
    if (!date) return value ? String(value) : "";
    return date.toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  function relativeTime(value) {
    const date = parseDate(value);
    if (!date) return "";
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    if (seconds < 86400 * 7) return `${Math.floor(seconds / 86400)} 天前`;
    return timeText(value);
  }

  function platformBadge(platform) {
    const key = (platform || "unknown").toLowerCase();
    return `<span class="badge ${esc(key)}">${esc(PLATFORM_LABELS[key] || platform || "其他")}</span>`;
  }

  function statusBadge(status, text) {
    const key = (status || "unknown").toLowerCase();
    const tone = STATUS_TONE[key] || "blue";
    return `<span class="badge status ${esc(tone)}">${esc(text || STATUS_LABELS[key] || status || "未知")}</span>`;
  }

  function snippet(text, length = 120) {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    return value.length > length ? value.slice(0, length) + "…" : value;
  }

  function empty(title, sub = "") {
    return `<div class="empty"><strong>${esc(title)}</strong>${esc(sub)}</div>`;
  }

  let toastTimer = null;
  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), 2600);
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    let response;
    try {
      response = await fetch(path, { ...options, headers });
    } catch (error) {
      throw new Error("网络不可用，检查 Tailscale 或后端是否启动");
    }
    if (response.status === 401) {
      showTokenModal();
      throw new Error("需要访问令牌");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      throw new Error(`服务返回异常 (${response.status})`);
    }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `请求失败 (${response.status})`);
    }
    return data;
  }

  // ------------------------------------------------------------------
  // Dashboard
  // ------------------------------------------------------------------
  async function loadConfig() {
    try {
      state.config = await api("/api/config");
      if (state.config.auth_required && !state.token) showTokenModal();
    } catch (error) {
      console.warn(error);
    }
  }

  async function loadDashboard({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    const refreshBtn = $("#refreshBtn");
    if (!silent) refreshBtn.classList.add("spinning");
    try {
      const [stats, orders, parcels, sms, captures, alerts] = await Promise.all([
        api("/api/stats"),
        api("/api/orders?limit=30"),
        api("/api/shipments?limit=100"),
        api("/api/sms?limit=30"),
        api("/api/captures?limit=20"),
        api("/api/alerts?limit=20"),
      ]);
      state.stats = stats;
      state.config = { ...(state.config || {}), ...stats };
      state.orders = orders.items || [];
      state.shipments = parcels.items || [];
      state.sms = sms.items || [];
      state.captures = captures.items || [];
      state.alerts = alerts.items || [];
      renderAll();
    } catch (error) {
      if (!String(error.message).includes("令牌")) toast(error.message);
    } finally {
      state.loading = false;
      refreshBtn.classList.remove("spinning");
    }
  }

  function renderAll() {
    renderSummary();
    renderRecent();
    renderActivity();
    renderParcels();
    renderHealth();
    renderRuns();
    renderSms();
    renderCaptures();
    renderTokenField();
    const stats = state.stats || {};
    const active = stats.active_shipments || 0;
    const count = stats.orders || 0;
    $("#brandSub").textContent = `${count} 单 · ${active} 个在途`;
  }

  function renderSummary() {
    const stats = state.stats || {};
    const ready = state.shipments.filter((item) => item.status === "ready_for_pickup").length;
    const unread = stats.alerts || 0;
    $("#summaryGrid").innerHTML = `
      <div class="summary-card accent"><div class="value">${stats.active_shipments || 0}</div><div class="label">在途包裹</div></div>
      <div class="summary-card warn"><div class="value">${ready}</div><div class="label">待取件</div></div>
      <div class="summary-card"><div class="value">${unread}</div><div class="label">未读提醒</div></div>
    `;
  }

  function orderCard(order) {
    const title = order.title || "(无商品名)";
    const sub = [order.shop, order.tracking_no ? `运单 ${order.tracking_no}` : ""]
      .filter(Boolean).join(" · ");
    const status = statusBadge(order.status, order.status_text);
    const amount = order.amount != null ? `<span class="card-amount">${esc(money(order.amount))}</span>` : "";
    return `
      <button type="button" class="card" data-order-id="${esc(order.id)}">
        <div class="thumb">📦</div>
        <div class="card-main">
          <div class="card-top">${platformBadge(order.platform)}${status}</div>
          <div class="card-title">${esc(title)}</div>
          ${sub ? `<div class="card-sub">${esc(sub)}</div>` : ""}
          <div class="card-bottom">${amount}<span class="card-time">${esc(timeText(order.order_time || order.created_at))}</span></div>
        </div>
      </button>`;
  }

  function renderRecent() {
    const list = $("#recentList");
    if (!state.orders.length) {
      list.innerHTML = empty("还没有订单", "短信和京东采集会慢慢把订单补进来。");
      $("#recentHint").textContent = "";
      return;
    }
    const orders = [...state.orders].sort((a, b) =>
      String(b.order_time || b.created_at || "").localeCompare(String(a.order_time || a.created_at || ""))
    );
    list.innerHTML = orders.map(orderCard).join("");
    $$(".card[data-order-id]", list).forEach((node) => {
      node.addEventListener("click", () => {
        const order = state.orders.find((item) => String(item.id) === node.dataset.orderId);
        if (order) openOrderSheet(order);
      });
    });
    $("#recentHint").textContent = `共 ${state.stats?.orders || orders.length} 单`;
  }

  function renderActivity() {
    const items = [
      ...state.sms.map((item) => ({
        kind: "sms", time: item.received_at || item.created_at,
        title: `${PLATFORM_LABELS[item.platform] || "短信"} · ${STATUS_LABELS[item.status] || item.status || "动态"}`,
        text: item.pickup_code ? `取件码 ${item.pickup_code} ${item.location || ""}` : snippet(item.raw_text, 100),
        tone: item.status === "delivered" ? "green" : item.status === "ready_for_pickup" ? "amber" : "blue",
      })),
      ...state.captures.map((item) => ({
        kind: "capture", time: item.captured_at || item.created_at,
        title: "抖音抓屏", text: snippet(item.text, 100), tone: "gray",
      })),
    ].filter((item) => item.time).sort((a, b) => String(b.time).localeCompare(String(a.time))).slice(0, 8);

    $("#recentActivity").innerHTML = items.length ? items.map((item) => `
      <div class="timeline-item">
        <div class="timeline-dot ${esc(item.tone)}"></div>
        <div class="timeline-body">
          <div class="timeline-title">${esc(item.title)}</div>
          <div class="timeline-text">${esc(item.text || "")}</div>
          <div class="timeline-time">${esc(relativeTime(item.time))}</div>
        </div>
      </div>`).join("") : empty("还没有动态", "短信转发和抖音抓屏会出现在这里。");
  }

  function shipmentCard(shipment) {
    const status = statusBadge(shipment.status, shipment.status_text);
    const meta = [shipment.carrier_name, shipment.tracking_no].filter(Boolean).join(" · ");
    const extra = shipment.pickup_code
      ? `取件码 <strong>${esc(shipment.pickup_code)}</strong>${shipment.location ? " · " + esc(shipment.location) : ""}`
      : (shipment.location || "");
    return `
      <button type="button" class="card" data-tracking="${esc(shipment.tracking_no)}">
        <div class="thumb">🚚</div>
        <div class="card-main">
          <div class="card-top">${status}${shipment.platform ? platformBadge(shipment.platform) : ""}</div>
          <div class="card-title">${esc(shipment.carrier_name || "包裹")} ${esc(shipment.tracking_no)}</div>
          ${extra ? `<div class="card-sub">${extra}</div>` : ""}
          <div class="card-bottom"><span class="card-sub">${esc(meta)}</span><span class="card-time">${esc(relativeTime(shipment.updated_at))}</span></div>
        </div>
      </button>`;
  }

  function renderParcels() {
    const list = $("#parcelList");
    const items = state.shipments
      .filter((item) => !state.parcelActive || !["delivered", "returned", "refunded"].includes(item.status))
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    if (!items.length) {
      list.innerHTML = empty(state.parcelActive ? "没有在途包裹" : "还没有包裹", "短信里的运单号会自动变成包裹卡片。");
      return;
    }
    list.innerHTML = items.map(shipmentCard).join("");
    $$(".card[data-tracking]", list).forEach((node) => {
      node.addEventListener("click", () => openParcelSheet(node.dataset.tracking));
    });
  }

  function renderHealth() {
    const config = state.config || {};
    $("#versionText").textContent = config.version ? `v${config.version}` : "";
    const rows = [
      ["访问令牌", config.auth_required ? "已开启" : "未设置", config.auth_required ? "ok" : "warn"],
      ["快递100", config.kuaidi100_configured ? "已配置" : "未配置", config.kuaidi100_configured ? "ok" : "off"],
      ["Bark 推送", config.bark_configured ? "已配置" : "未配置", config.bark_configured ? "ok" : "off"],
      ["京东采集", config.jd_collector_enabled ? "已开启" : "关闭", config.jd_collector_enabled ? "ok" : "off"],
      ["后台调度", config.scheduler_enabled ? "运行中" : "关闭", config.scheduler_enabled ? "ok" : "off"],
      ["数据存储", "本地 SQLite", "ok"],
    ];
    $("#healthGrid").innerHTML = rows.map(([k, v, tone]) => `
      <div class="health-item"><div class="k">${esc(k)}</div><div class="v ${esc(tone)}">${esc(v)}</div></div>
    `).join("");
  }

  function renderRuns() {
    const runs = state.stats?.collector_runs || [];
    $("#runsList").innerHTML = runs.length ? runs.map((run) => {
      const captcha = Number(run.captcha) ? `<span class="bad">验证码</span>` : "";
      return `
        <div class="run">
          <div class="platform">${esc((run.platform || "").toUpperCase())}</div>
          <div class="meta"><div>${esc(run.status || "")} · ${esc(run.items || 0)} 条</div><div>${esc(timeText(run.started_at))} ${esc(run.message || "")}</div></div>
          <div class="stat">${captcha}<br>${esc(run.finished_at ? "" : "运行中")}</div>
        </div>`;
    }).join("") : `<div class="muted small">还没有采集记录。开启京东采集后会在这里统计验证码次数。</div>`;
  }

  function renderSms() {
    const items = state.sms.slice(0, 20);
    $("#smsList").innerHTML = items.length ? items.map((item) => `
      <div class="timeline-item">
        <div class="timeline-dot ${item.status === "delivered" ? "green" : item.status === "ready_for_pickup" ? "amber" : "gray"}"></div>
        <div class="timeline-body">
          <div class="timeline-title">${esc(PLATFORM_LABELS[item.platform] || "短信")} · ${esc(item.status_text || STATUS_LABELS[item.status] || item.status || "")}</div>
          <div class="timeline-text">${esc(snippet(item.raw_text, 140))}</div>
          <div class="timeline-time">${esc(timeText(item.received_at))}${item.tracking_no ? " · " + esc(item.tracking_no) : ""}</div>
        </div>
      </div>`).join("") : `<div class="muted small">还没有短信。先在 iPhone 上配置短信自动化。</div>`;
  }

  function renderCaptures() {
    const items = state.captures.slice(0, 12);
    $("#captureList").innerHTML = items.length ? items.map((item) => `
      <div class="card">
        <div class="thumb">📸</div>
        <div class="card-main">
          <div class="card-top">${platformBadge(item.platform)}<span class="card-time">${esc(relativeTime(item.captured_at || item.created_at))}</span></div>
          <div class="card-title">${esc(snippet(item.text, 90))}</div>
        </div>
      </div>`).join("") : `<div class="muted small">还没有抓屏。抖音订单详情可以用快捷指令抓进来。</div>`;
  }

  // ------------------------------------------------------------------
  // Search
  // ------------------------------------------------------------------
  let searchTimer = null;
  async function runSearch() {
    const query = state.searchQuery.trim();
    if (!query) {
      state.searchResults = [];
      $("#searchList").innerHTML = empty("搜点什么", "商品名、店铺、运单号、短信内容都可以搜。");
      $("#searchMeta").textContent = "";
      return;
    }
    $("#searchMeta").textContent = "搜索中…";
    try {
      const params = new URLSearchParams({ q: query, limit: "50" });
      if (state.platform) params.set("platform", state.platform);
      const data = await api(`/api/search?${params}`);
      state.searchResults = data.items || [];
      renderSearch();
    } catch (error) {
      $("#searchMeta").textContent = error.message;
    }
  }

  function renderSearch() {
    const list = $("#searchList");
    if (!state.searchResults.length) {
      list.innerHTML = empty("没有找到", "换个关键词，或等季度个人信息副本导入。");
      $("#searchMeta").textContent = "0 条结果";
      return;
    }
    $("#searchMeta").textContent = `${state.searchResults.length} 条结果`;
    list.innerHTML = state.searchResults.map((item, index) => {
      if (item.doc_type === "order" && item.order) {
        const order = item.order;
        return `
          <button type="button" class="card" data-search-index="${index}">
            <div class="thumb">📦</div>
            <div class="card-main">
              <div class="card-top">${platformBadge(order.platform)}${statusBadge(order.status, order.status_text)}</div>
              <div class="card-title">${esc(order.title || "(无商品名)")}</div>
              <div class="card-sub">${esc([order.shop, order.tracking_no].filter(Boolean).join(" · "))}</div>
              <div class="card-bottom"><span class="card-amount">${esc(money(order.amount))}</span><span class="card-time">${esc(timeText(order.order_time))}</span></div>
            </div>
          </button>`;
      }
      const title = item.doc_type === "capture" ? "抓屏" : "短信";
      return `
        <button type="button" class="card" data-search-index="${index}">
          <div class="thumb">${item.doc_type === "capture" ? "📸" : "💬"}</div>
          <div class="card-main">
            <div class="card-top">${platformBadge(item.platform)}<span class="badge">${esc(title)}</span></div>
            <div class="card-title">${esc(snippet(item.snippet || "", 120))}</div>
            <div class="card-time">${esc(timeText(item.occurred_at))}</div>
          </div>
        </button>`;
    }).join("");
    $$(".card[data-search-index]", list).forEach((node) => {
      node.addEventListener("click", () => {
        const item = state.searchResults[Number(node.dataset.searchIndex)];
        if (item?.order) openOrderSheet(item.order);
        else openTextSheet(item);
      });
    });
  }

  // ------------------------------------------------------------------
  // Detail sheets
  // ------------------------------------------------------------------
  function openSheet(html) {
    $("#sheetContent").innerHTML = html;
    $("#sheet").classList.remove("hidden");
    $("#sheetBackdrop").classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }

  function closeSheet() {
    $("#sheet").classList.add("hidden");
    $("#sheetBackdrop").classList.add("hidden");
    document.body.style.overflow = "";
  }

  function kv(rows) {
    return `<div class="kv">${rows.filter(([, value]) => value != null && value !== "").map(([key, value]) => `
      <div class="k">${esc(key)}</div><div class="v">${value}</div>`).join("")}</div>`;
  }

  function openOrderSheet(order) {
    const url = order.url ? `<a href="${esc(order.url)}" target="_blank" rel="noreferrer">打开订单</a>` : "";
    openSheet(`
      <h2>${esc(order.title || "(无商品名)")}</h2>
      <div class="card-top">${platformBadge(order.platform)}${statusBadge(order.status, order.status_text)}</div>
      ${kv([
        ["店铺", esc(order.shop || "")],
        ["金额", order.amount != null ? esc(money(order.amount)) : ""],
        ["下单时间", esc(timeText(order.order_time))],
        ["运单号", esc(order.tracking_no || "")],
        ["订单号", esc(order.platform_order_id || "")],
        ["链接", url],
      ])}
      ${order.raw_json ? `<div class="raw">${esc(String(order.raw_json).slice(0, 3000))}</div>` : ""}
    `);
  }

  async function openParcelSheet(trackingNo) {
    openSheet(`<h2>包裹 ${esc(trackingNo)}</h2><div class="muted small">加载物流轨迹…</div>`);
    try {
      const data = await api(`/api/shipments/${encodeURIComponent(trackingNo)}`);
      const item = data.item;
      const events = item.events || [];
      openSheet(`
        <h2>${esc(item.carrier_name || "包裹")}</h2>
        <div class="card-top">${statusBadge(item.status, item.status_text)}${item.platform ? platformBadge(item.platform) : ""}</div>
        ${kv([
          ["运单号", esc(item.tracking_no)],
          ["取件码", item.pickup_code ? `<strong>${esc(item.pickup_code)}</strong>` : ""],
          ["位置", esc(item.location || "")],
          ["更新时间", esc(timeText(item.updated_at))],
        ])}
        <div class="event-list">
          ${events.length ? events.map((event) => `
            <div class="event"><time>${esc(timeText(event.event_time))}</time><div>${esc(event.context || "")}</div></div>
          `).join("") : `<div class="muted small">还没有轨迹。配置快递100 后会自动补全。</div>`}
        </div>
      `);
    } catch (error) {
      openSheet(`<h2>包裹 ${esc(trackingNo)}</h2><div class="muted small">${esc(error.message)}</div>`);
    }
  }

  function openTextSheet(item) {
    const body = item.sms?.raw_text || item.capture?.text || item.snippet || "";
    openSheet(`
      <h2>${item.doc_type === "capture" ? "抖音抓屏" : "短信动态"}</h2>
      <div class="card-top">${platformBadge(item.platform)}</div>
      <div class="raw">${esc(body)}</div>
      <div class="muted small" style="margin-top:10px">${esc(timeText(item.occurred_at))}</div>
    `);
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  async function pollParcels() {
    if (!state.config?.kuaidi100_configured) {
      toast("先配置 KUAIDI100_CUSTOMER / KEY");
      return;
    }
    try {
      await api("/api/parcels/poll", { method: "POST", body: "{}" });
      toast("已开始刷新包裹");
      setTimeout(() => loadDashboard({ silent: true }), 4000);
    } catch (error) { toast(error.message); }
  }

  async function collectJd() {
    if (!state.config?.jd_collector_enabled) {
      toast("先在 .env 打开 ENABLE_JD_COLLECTOR");
      return;
    }
    try {
      await api("/api/collectors/jd/run", { method: "POST", body: "{}" });
      toast("已开始京东采集，稍后看结果");
      setTimeout(() => loadDashboard({ silent: true }), 12000);
    } catch (error) { toast(error.message); }
  }

  function openManualForm() {
    const localNow = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
      .toISOString().slice(0, 16);
    openSheet(`
      <h2>手动添加订单</h2>
      <p class="muted small" style="margin-top:-2px;margin-bottom:14px">补美团外卖、抖音，或任何没被自动抓到的订单。</p>
      <form id="manualForm">
        <label class="field"><span>平台</span>
          <select id="mPlatform">
            <option value="meituan">美团</option>
            <option value="douyin">抖音</option>
            <option value="jd">京东</option>
            <option value="taobao">淘宝</option>
            <option value="pdd">拼多多</option>
            <option value="other">其他</option>
          </select>
        </label>
        <label class="field"><span>商品 / 内容</span><input id="mTitle" required placeholder="例如：麻辣烫套餐"></label>
        <div class="field-row">
          <label class="field"><span>店铺</span><input id="mShop" placeholder="可选"></label>
          <label class="field"><span>金额</span><input id="mAmount" inputmode="decimal" placeholder="0.00"></label>
        </div>
        <div class="field-row">
          <label class="field"><span>下单时间</span><input id="mTime" type="datetime-local" value="${esc(localNow)}"></label>
          <label class="field"><span>运单号</span><input id="mTracking" placeholder="可选"></label>
        </div>
        <label class="field"><span>订单号</span><input id="mOrderId" placeholder="可选"></label>
        <label class="field"><span>链接</span><input id="mUrl" type="url" placeholder="可选"></label>
        <div class="button-row"><button class="btn primary" type="submit">保存订单</button></div>
      </form>
    `);
    $("#manualForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const title = $("#mTitle").value.trim();
      if (!title) { toast("填个商品名"); return; }
      const timeValue = $("#mTime").value;
      const payload = {
        platform: $("#mPlatform").value,
        title,
        shop: $("#mShop").value.trim(),
        amount: $("#mAmount").value.trim() || null,
        status: "unknown",
        status_text: "手动添加",
        order_time: timeValue ? new Date(timeValue).toISOString() : null,
        tracking_no: $("#mTracking").value.trim().toUpperCase() || null,
        platform_order_id: $("#mOrderId").value.trim() || null,
        url: $("#mUrl").value.trim() || null,
      };
      try {
        await api("/api/orders", { method: "POST", body: JSON.stringify(payload) });
        closeSheet();
        toast("已添加");
        loadDashboard({ silent: true });
      } catch (error) { toast(error.message); }
    });
  }

  async function exportData() {
    try {
      const data = await api("/api/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `orderhub-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 2000);
      toast("已导出");
    } catch (error) { toast(error.message); }
  }

  function showTokenModal() {
    $("#tokenBackdrop").classList.remove("hidden");
    $("#tokenModalInput").value = state.token;
    setTimeout(() => $("#tokenModalInput").focus(), 50);
  }

  function saveTokenFrom(value) {
    state.token = (value || "").trim();
    if (state.token) localStorage.setItem("orderhub_token", state.token);
    else localStorage.removeItem("orderhub_token");
    $("#tokenBackdrop").classList.add("hidden");
    loadDashboard();
  }

  function renderTokenField() {
    $("#tokenInput").value = state.token;
  }

  function switchTab(tab) {
    state.tab = tab;
    $$(".view").forEach((node) => node.classList.add("hidden"));
    $(`#view-${tab}`).classList.remove("hidden");
    $$(".tab").forEach((node) => node.classList.toggle("active", node.dataset.tab === tab));
    if (tab === "search") {
      setTimeout(() => $("#searchInput").focus(), 50);
      if (!state.searchQuery) {
        $("#searchList").innerHTML = empty("搜点什么", "商品名、店铺、运单号、短信内容都可以搜。");
      }
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ------------------------------------------------------------------
  // Bindings
  // ------------------------------------------------------------------
  function bind() {
    $$(".tab").forEach((node) => node.addEventListener("click", () => switchTab(node.dataset.tab)));
    $("#refreshBtn").addEventListener("click", () => loadDashboard());

    $$("#parcelFilters .chip").forEach((node) => node.addEventListener("click", () => {
      state.parcelActive = node.dataset.active === "1";
      $$("#parcelFilters .chip").forEach((item) => item.classList.toggle("active", item === node));
      renderParcels();
    }));

    $$("#platformFilters .chip").forEach((node) => node.addEventListener("click", () => {
      state.platform = node.dataset.platform || "";
      $$("#platformFilters .chip").forEach((item) => item.classList.toggle("active", item === node));
      runSearch();
    }));

    $("#searchInput").addEventListener("input", (event) => {
      state.searchQuery = event.target.value;
      $("#clearSearch").classList.toggle("hidden", !state.searchQuery);
      clearTimeout(searchTimer);
      searchTimer = setTimeout(runSearch, 250);
    });
    $("#searchInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); runSearch(); }
    });
    $("#clearSearch").addEventListener("click", () => {
      state.searchQuery = "";
      $("#searchInput").value = "";
      $("#clearSearch").classList.add("hidden");
      runSearch();
    });

    $("#sheetBackdrop").addEventListener("click", closeSheet);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSheet(); });

    $("#pollParcelsBtn").addEventListener("click", pollParcels);
    $("#collectJdBtn").addEventListener("click", collectJd);
    $("#manualAddBtn").addEventListener("click", openManualForm);
    $("#exportBtn").addEventListener("click", exportData);
    $("#saveTokenBtn").addEventListener("click", () => saveTokenFrom($("#tokenInput").value));
    $("#clearTokenBtn").addEventListener("click", () => saveTokenFrom(""));
    $("#tokenModalSave").addEventListener("click", () => saveTokenFrom($("#tokenModalInput").value));
    $("#tokenModalInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") saveTokenFrom(event.target.value);
    });
  }

  async function init() {
    bind();
    $("#recentList").innerHTML = `<div class="skeleton"></div><div class="skeleton" style="margin-top:10px"></div>`;
    $("#parcelList").innerHTML = `<div class="skeleton"></div>`;
    await loadConfig();
    if (state.config?.auth_required && !state.token) {
      showTokenModal();
    } else {
      await loadDashboard();
    }
    if ("serviceWorker" in navigator) {
      const hadController = Boolean(navigator.serviceWorker.controller);
      let reloading = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (!hadController || reloading) return;
        reloading = true;
        window.location.reload();
      });
      navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {});
    }
    setInterval(() => {
      if (!document.hidden && (!state.config?.auth_required || state.token)) {
        loadDashboard({ silent: true });
      }
    }, 120000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
