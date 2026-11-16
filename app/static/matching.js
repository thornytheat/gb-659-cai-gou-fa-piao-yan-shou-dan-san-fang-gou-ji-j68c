/* M5 多对多匹配 · 撤销补偿台账：接入真实单据与全部写操作 */
const $ = (selector) => document.querySelector(selector);
const result = $("#result");
const feedback = $("#feedback");
const money = (value) => (value === undefined || value === null ? "—" : String(value));

const STATE_LABEL = {
  ACTIVE: "占用中",
  RELEASED: "已释放",
  PAID: "已付款",
  PARTIAL_RETAINED: "部分保留",
  RETAINED: "已保留待追回",
  PENDING: "待追回",
  SETTLED: "已结清",
  REVOKED: "已撤销",
};

function setText(selector, value) {
  const node = $(selector);
  if (node) node.textContent = value;
}

function statusBadge(status) {
  const tone = ["RELEASED", "SETTLED"].includes(status)
    ? "state--muted"
    : ["REVOKED", "PARTIAL_RETAINED", "RETAINED", "PENDING"].includes(status)
      ? "state--warn"
      : "state--ok";
  return `<span class="state ${tone}">${STATE_LABEL[status] || status}</span>`;
}

function row(cells) {
  return `<tr>${cells.join("")}</tr>`;
}
function td(value, numeric = false) {
  return `<td class="${numeric ? "num" : ""}">${value ?? ""}</td>`;
}

async function callApi(path, payload) {
  const options = payload
    ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) }
    : { method: "GET" };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail || {};
    throw new Error(`${detail.code || response.status}：${detail.message || "请求失败"}`);
  }
  return data;
}

async function refresh() {
  try {
    const data = await callApi("/api/matching/ledger");
    renderKpis(data);
    renderGr(data.goods_receipts);
    renderInvoices(data.invoices);
    renderLinks(data.match_links);
    renderRecoveries(data.recoveries);
    renderInvariant(data.invariants);
    if (feedback) feedback.textContent = "台账已刷新";
  } catch (error) {
    result.textContent = `加载失败：${error.message}`;
  }
}

function renderKpis(data) {
  setText("#kpi-payable", money(data.kpis.current_payable));
  setText("#kpi-original", money(data.kpis.original_total));
  setText("#kpi-revoked", money(data.kpis.revoked_total));
  setText("#kpi-pending", money(data.kpis.pending_recovery));
  setText("#kpi-settled", money(data.kpis.settled_recovery));
}

function renderInvariant(invariants) {
  const badge = $("#invariant-badge");
  if (!badge) return;
  badge.textContent = invariants.ok ? "守恒对账通过" : `异常 ${invariants.problems.length} 项`;
  badge.className = `panel__meta ${invariants.ok ? "ok-text" : "warn-text"}`;
}

function renderGr(rows) {
  const body = $("#gr-body");
  if (!rows.length) {
    body.innerHTML = row([`<td colspan="8" class="empty">暂无单据，点击顶部「载入演示单据」</td>`]);
    return;
  }
  body.innerHTML = rows
    .map((item) =>
      row([
        td(item.gr_id),
        td(`${item.sku} / ${item.tax_rate}`),
        td(money(item.original_amount), true),
        td(money(item.occupied_amount), true),
        td(`<b>${money(item.available_amount)}</b>`, true),
        td(`${money(item.paid_amount)} / ${money(item.retained_amount)}`, true),
        td(`v${item.version}`),
        td(statusBadge(item.status)),
      ])
    )
    .join("");
}

function renderInvoices(rows) {
  const body = $("#iv-body");
  body.innerHTML = rows.length
    ? rows
        .map((item) =>
          row([
            td(item.invoice_id),
            td(`${item.sku} / ${item.tax_rate}`),
            td(money(item.original_amount), true),
            td(money(item.occupied_amount), true),
            td(`<b>${money(item.available_amount)}</b>`, true),
            td(money(item.paid_amount), true),
          ])
        )
        .join("")
    : row([`<td colspan="6" class="empty">暂无发票</td>`]);
}

function renderLinks(rows) {
  const body = $("#link-body");
  if (!rows.length) {
    body.innerHTML = row([`<td colspan="9" class="empty">暂无匹配凭证</td>`]);
    return;
  }
  body.innerHTML = rows
    .map((item) => {
      const payable = item.status === "ACTIVE";
      const unpaid = Number(item.amount) - Number(item.paid_amount) - Number(item.released_amount);
      const action = payable
        ? `<button class="btn btn--mini" data-pay="${item.link_id}" data-unpaid="${unpaid.toFixed(2)}">登记付款</button>`
        : `<span class="panel__meta">凭证保留</span>`;
      return row([
        td(item.link_id),
        td(item.gr_id),
        td(item.invoice_id),
        td(money(item.amount), true),
        td(money(item.paid_amount), true),
        td(money(item.released_amount), true),
        td(money(item.retained_amount), true),
        td(statusBadge(item.status)),
        td(action),
      ]);
    })
    .join("");
}

function renderRecoveries(rows) {
  const body = $("#recovery-body");
  if (!rows.length) {
    body.innerHTML = row([`<td colspan="6" class="empty">暂无待追回</td>`]);
    return;
  }
  body.innerHTML = rows
    .map((item) => {
      const settle =
        item.status === "PENDING"
          ? `<button class="btn btn--mini btn--warn" data-credit="${item.recovery_id}" data-outstanding="${item.outstanding_amount}">红字冲抵</button>`
          : `<span class="panel__meta">已结清</span>`;
      return row([
        td(item.recovery_id),
        td(item.link_id),
        td(money(item.amount), true),
        td(`<b>${money(item.outstanding_amount)}</b>`, true),
        td(statusBadge(item.status)),
        td(settle),
      ]);
    })
    .join("");
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

const FORM_ENDPOINTS = {
  seed: () => ["/api/matching/seed", {}],
  gr: (d) => ["/api/matching/goods-receipts", d],
  invoice: (d) => ["/api/matching/invoices", d],
  match: (d) => ["/api/matching/matches", d],
  revoke: (d) => [
    "/api/matching/revocations",
    {
      gr_id: d.gr_id,
      idempotency_key: d.idempotency_key,
      expected_version: d.expected_version ? Number(d.expected_version) : null,
    },
  ],
};

document.querySelectorAll("form[data-action]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const action = form.dataset.action;
    try {
      if (action === "resume") {
        const data = await callApi("/api/matching/revocations/resume", {});
        result.textContent = JSON.stringify(data, null, 2);
      } else {
        const [path, payload] = FORM_ENDPOINTS[action](formData(form));
        const data = await callApi(path, payload);
        result.textContent = JSON.stringify(data, null, 2);
        form.reset();
      }
      if (feedback) feedback.textContent = "操作成功";
      await refresh();
    } catch (error) {
      result.textContent = `操作失败（原匹配保留，可重试）：${error.message}`;
      if (feedback) feedback.textContent = "失败，可重试";
    }
  });
});

/* 行内付款登记：仅 ACTIVE 凭证可付款，幂等键自动生成 */
document.addEventListener("click", async (event) => {
  const payBtn = event.target.closest("[data-pay]");
  const creditBtn = event.target.closest("[data-credit]");
  try {
    if (payBtn) {
      const amount = window.prompt(`登记付款金额（未付 ${payBtn.dataset.unpaid}）`, payBtn.dataset.unpaid);
      if (amount === null) return;
      const key = `PAY-${payBtn.dataset.pay}-${Date.now()}`;
      const data = await callApi("/api/matching/payments", {
        link_id: payBtn.dataset.pay,
        amount,
        idempotency_key: key,
      });
      result.textContent = JSON.stringify(data, null, 2);
      await refresh();
    } else if (creditBtn) {
      const amount = window.prompt(`红字冲抵金额（待追回余额 ${creditBtn.dataset.outstanding}）`, creditBtn.dataset.outstanding);
      if (amount === null) return;
      const key = window.prompt("红字冲抵幂等键（重试不重复冲抵）", `CN-${Date.now()}`);
      if (!key) return;
      const data = await callApi("/api/matching/credit-notes", {
        recovery_id: creditBtn.dataset.credit,
        amount,
        idempotency_key: key,
      });
      result.textContent = JSON.stringify(data, null, 2);
      await refresh();
    }
  } catch (error) {
    result.textContent = `操作失败（原匹配保留，可重试）：${error.message}`;
  }
});

/* 顶部按钮：首次载入演示单据，之后仅刷新台账 */
const runButton = $("#run");
if (runButton) {
  runButton.textContent = "刷新台账";
  runButton.addEventListener("click", refresh);
}
refresh();
