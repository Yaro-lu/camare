const deviceList = document.querySelector("#deviceList");
const historyList = document.querySelector("#historyList");
const historyCount = document.querySelector("#historyCount");
const pairDialog = document.querySelector("#pairDialog");
const pairForm = document.querySelector("#pairForm");
const qrResult = document.querySelector("#qrResult");
const qrImage = document.querySelector("#qrImage");
const toast = document.querySelector("#adminToast");
let currentPairURL = "";

document.querySelector("#openPairing").addEventListener("click", () => {
  pairForm.hidden = false;
  qrResult.hidden = true;
  pairDialog.showModal();
});
document.querySelector("#refresh").addEventListener("click", refresh);
document.querySelector("#qrClose").addEventListener("click", () => pairDialog.close());
document.querySelector("#copyPairURL").addEventListener("click", copyPairURL);
pairForm.addEventListener("submit", generatePairing);
pairDialog.addEventListener("click", (event) => {
  if (event.target === pairDialog) pairDialog.close();
});

refresh();
setInterval(refresh, 30000);

async function refresh() {
  try {
    const [devicesResponse, pairingsResponse] = await Promise.all([
      fetch("/api/devices", {credentials: "same-origin"}),
      fetch("/api/pairings", {credentials: "same-origin"}),
    ]);
    if (!devicesResponse.ok || !pairingsResponse.ok) throw new Error("读取失败");
    const devices = (await devicesResponse.json()).devices || [];
    const pairings = (await pairingsResponse.json()).pairings || [];
    renderDevices(devices);
    document.querySelector("#activeCount").textContent = devices.filter((item) => !item.revoked_at).length;
    document.querySelector("#revokedCount").textContent = devices.filter((item) => item.revoked_at).length;
    const now = Date.now();
    document.querySelector("#pendingCount").textContent = pairings.filter((item) =>
      !item.redeemed_at && new Date(item.expires_at).getTime() > now
    ).length;
  } catch {
    deviceList.replaceChildren(makeElement("p", "empty", "设备信息读取失败，请刷新重试。"));
    historyList.replaceChildren(makeElement("p", "empty", "历史记录读取失败，请刷新重试。"));
    historyCount.textContent = "—";
  }
}

function renderDevices(devices) {
  const activeDevices = devices.filter((item) => !item.revoked_at);
  const revokedDevices = devices
    .filter((item) => item.revoked_at)
    .sort((left, right) => new Date(right.revoked_at) - new Date(left.revoked_at));

  deviceList.replaceChildren();
  if (!activeDevices.length) {
    deviceList.append(makeElement("p", "empty", "暂无未撤销设备。"));
  } else {
    for (const device of activeDevices) {
      deviceList.append(renderDevice(device, false));
    }
  }

  historyList.replaceChildren();
  historyCount.textContent = `${revokedDevices.length} 条`;
  if (!revokedDevices.length) {
    historyList.append(makeElement("p", "empty", "还没有已撤销的设备。"));
  } else {
    for (const device of revokedDevices) {
      historyList.append(renderDevice(device, true));
    }
  }
}

function renderDevice(device, isHistory) {
    const card = makeElement("article", `device-card${isHistory ? " history-card" : ""}`);
    const icon = makeElement("div", "device-icon", platformGlyph(device.platform));
    const main = makeElement("div", "device-main");
    const heading = makeElement("div", "device-heading");
    heading.append(
      makeElement("h3", "", device.label || device.model || device.platform || "未命名设备"),
      makeElement("span", isHistory ? "status revoked-status" : "status", isHistory ? "已撤销" : "未撤销")
    );
    const description = [
      device.model && device.model !== device.platform ? device.model : "",
      device.os,
      device.browser,
    ].filter(Boolean).join(" · ");
    const detail = makeElement("p", "device-detail", description || "浏览器未提供机型信息");
    const meta = makeElement("p", "device-meta");
    meta.textContent = `${isHistory ? `撤销 ${formatTime(device.revoked_at)} · ` : ""}首次 ${formatTime(device.first_seen)} · 最近 ${formatTime(device.last_seen)}${device.last_ip ? ` · ${device.last_ip}` : ""}`;
    main.append(heading, detail, meta);

    card.append(icon, main);
    if (!isHistory) {
      const actions = makeElement("div", "device-actions");
      const rename = makeElement("button", "text-button", "重命名");
      rename.type = "button";
      rename.addEventListener("click", () => renameDevice(device));
      const revoke = makeElement("button", "danger-button", "撤销");
      revoke.type = "button";
      revoke.addEventListener("click", () => revokeDevice(device));
      actions.append(rename, revoke);
      card.append(actions);
    }
    return card;
}

async function generatePairing(event) {
  event.preventDefault();
  const button = document.querySelector("#generatePairing");
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    const response = await adminFetch("/api/pairings", {
      method: "POST",
      body: JSON.stringify({
        label: document.querySelector("#pairLabel").value,
        expiry_hours: Number(document.querySelector("#pairExpiry").value),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "生成失败");
    currentPairURL = payload.url;
    qrImage.src = payload.qr;
    pairForm.hidden = true;
    qrResult.hidden = false;
    refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "生成二维码";
  }
}

async function renameDevice(device) {
  const label = prompt("设备名称", device.label || "");
  if (label === null || !label.trim()) return;
  const response = await adminFetch(`/api/devices/${encodeURIComponent(device.id)}`, {
    method: "PATCH",
    body: JSON.stringify({label: label.trim()}),
  });
  if (response.ok) {
    showToast("名称已更新");
    refresh();
  } else {
    showToast("名称更新失败");
  }
}

async function revokeDevice(device) {
  if (!confirm(`撤销“${device.label || "此设备"}”的观看权限？`)) return;
  const response = await adminFetch(`/api/devices/${encodeURIComponent(device.id)}`, {
    method: "DELETE",
  });
  if (response.ok) {
    showToast("设备已撤销");
    refresh();
  } else {
    showToast("撤销失败");
  }
}

async function copyPairURL() {
  if (!currentPairURL) return;
  try {
    await navigator.clipboard.writeText(currentPairURL);
    showToast("绑定链接已复制");
  } catch {
    showToast("无法自动复制，请使用二维码");
  }
}

function adminFetch(url, options) {
  return fetch(url, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Camera-Admin": "1",
      ...(options.headers || {}),
    },
  });
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function platformGlyph(platform = "") {
  if (/iPhone|iPad/i.test(platform)) return "i";
  if (/Android/i.test(platform)) return "A";
  if (/Windows/i.test(platform)) return "W";
  if (/macOS|Mac/i.test(platform)) return "M";
  return "•";
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

let toastTimer;
function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
}
