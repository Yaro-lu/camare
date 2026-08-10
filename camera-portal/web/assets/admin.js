const deviceList = document.querySelector("#deviceList");
const historyList = document.querySelector("#historyList");
const historyCount = document.querySelector("#historyCount");
const pairDialog = document.querySelector("#pairDialog");
const pairForm = document.querySelector("#pairForm");
const qrResult = document.querySelector("#qrResult");
const qrImage = document.querySelector("#qrImage");
const toast = document.querySelector("#adminToast");
const cameraGroupsElement = document.querySelector("#cameraGroups");
const applyCameraAudioButton = document.querySelector("#applyCameraAudio");
const cameraApplyStatus = document.querySelector("#cameraApplyStatus");
let currentPairURL = "";
const cameraSettings = {
  groups: [],
  currentAudio: new Map(),
  pendingAudio: new Map(),
  groupElements: new Map(),
  applying: false,
};

document.querySelector("#openPairing").addEventListener("click", () => {
  pairForm.hidden = false;
  qrResult.hidden = true;
  pairDialog.showModal();
});
document.querySelector("#refresh").addEventListener("click", refresh);
document.querySelector("#qrClose").addEventListener("click", () => pairDialog.close());
document.querySelector("#copyPairURL").addEventListener("click", copyPairURL);
applyCameraAudioButton.addEventListener("click", applyCameraAudioSettings);
pairForm.addEventListener("submit", generatePairing);
pairDialog.addEventListener("click", (event) => {
  if (event.target === pairDialog) pairDialog.close();
});

loadCameraGroups();
refresh();
setInterval(refresh, 30000);

async function loadCameraGroups() {
  cameraGroupsElement.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/camera-groups", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error("摄像头信息读取失败");

    const payload = await response.json();
    cameraSettings.groups = normalizeCameraGroups(payload);
    cameraSettings.currentAudio.clear();
    cameraSettings.pendingAudio.clear();
    for (const group of cameraSettings.groups) {
      cameraSettings.currentAudio.set(group.id, group.recordAudio);
      cameraSettings.pendingAudio.set(group.id, group.recordAudio);
    }
    renderCameraGroups();
    updateCameraApplyState();
  } catch (error) {
    cameraGroupsElement.replaceChildren(
      makeElement("p", "empty camera-empty", error.message || "摄像头信息读取失败，请刷新页面重试。")
    );
    cameraApplyStatus.textContent = "摄像头设置不可用，请刷新页面重试。";
    cameraApplyStatus.dataset.tone = "error";
  } finally {
    cameraGroupsElement.setAttribute("aria-busy", "false");
  }
}

function normalizeCameraGroups(payload) {
  const rawGroups = Array.isArray(payload) ? payload : payload.groups;
  if (!Array.isArray(rawGroups)) throw new Error("摄像头分组数据格式不正确");

  return rawGroups.map((group, groupIndex) => {
    const id = String(group.id || group.ID || group.key || `group-${groupIndex + 1}`);
    const cameraCandidates = group.cameras || group.Cameras || group.camera_ids || group.cameraIds;
    const rawCameras = Array.isArray(cameraCandidates) ? cameraCandidates : [];
    const cameras = rawCameras.map((camera, cameraIndex) => normalizeCamera(camera, cameraIndex));
    while (cameras.length < 2) cameras.push(normalizeCamera(null, cameras.length));
    const rawRecordAudio = group.record_audio !== undefined
      ? group.record_audio
      : group.recordAudio !== undefined
        ? group.recordAudio
        : group.RecordAudio;

    return {
      id,
      name: group.name || group.Name || group.label || `第 ${groupIndex + 1} 组`,
      recordAudio: rawRecordAudio === null ? null : Boolean(rawRecordAudio),
      cameras,
      hasConnectedCameras: cameras.some((camera) => !camera.pending),
    };
  });
}

function normalizeCamera(camera, cameraIndex) {
  if (typeof camera === "string") {
    return {
      id: camera,
      name: camera || `待接入画面 ${cameraIndex + 1}`,
      pending: !camera,
    };
  }
  if (!camera) {
    return {id: "", name: `待接入画面 ${cameraIndex + 1}`, pending: true};
  }
  const id = String(camera.id || camera.ID || camera.key || "");
  return {
    id,
    name: camera.name || camera.Name || camera.label || id || `待接入画面 ${cameraIndex + 1}`,
    pending: camera.pending === true || camera.Pending === true || camera.connected === false || camera.available === false || !id,
  };
}

function renderCameraGroups() {
  cameraGroupsElement.replaceChildren();
  cameraSettings.groupElements.clear();

  if (!cameraSettings.groups.length) {
    cameraGroupsElement.append(makeElement("p", "empty camera-empty", "暂未配置摄像头分组。"));
    return;
  }

  for (const group of cameraSettings.groups) {
    const card = makeElement("article", `camera-group${group.hasConnectedCameras ? "" : " no-cameras"}`);
    card.dataset.groupId = group.id;

    const head = makeElement("div", "camera-group-head");
    const title = makeElement("div", "camera-group-title");
    title.append(
      makeElement("h3", "", group.name),
      makeElement("p", "", group.hasConnectedCameras ? "同组两个画面 · 整组录像设置" : "两个画面均待接入")
    );
    const state = makeElement("span", "camera-group-state", group.hasConnectedCameras ? "已应用" : "待接入");
    head.append(title, state);

    const views = makeElement("div", "camera-group-views");
    for (const camera of group.cameras.slice(0, 2)) {
      views.append(renderCameraView(camera));
    }

    const audioRow = makeElement("div", "camera-audio-row");
    const audioCopy = makeElement("div", "camera-audio-copy");
    const audioHint = makeElement(
      "span",
      "",
      group.hasConnectedCameras ? "关闭可减少音频转码占用" : "接入摄像头后可设置"
    );
    audioCopy.append(
      makeElement("strong", "", "录像包含声音"),
      audioHint
    );
    const switchLabel = makeElement("label", "audio-switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = group.recordAudio === true;
    input.indeterminate = group.recordAudio === null;
    input.disabled = !group.hasConnectedCameras;
    input.setAttribute("aria-label", `${group.name}录像包含声音`);
    input.addEventListener("change", () => {
      if (!group.hasConnectedCameras) return;
      input.indeterminate = false;
      cameraSettings.pendingAudio.set(group.id, input.checked);
      updateCameraApplyState();
    });
    switchLabel.append(input, makeElement("span", "audio-switch-track"));
    audioRow.append(audioCopy, switchLabel);

    card.append(head, views, audioRow);
    cameraGroupsElement.append(card);
    cameraSettings.groupElements.set(group.id, {card, state, input, audioHint});
  }
}

function renderCameraView(camera) {
  const view = makeElement("figure", `camera-view${camera.pending ? " pending-slot" : ""}`);
  const state = makeElement("div", "camera-view-state", camera.pending ? "待接入" : "正在获取快照…");
  const label = makeElement("figcaption", "camera-view-label", camera.name);
  view.append(state, label);

  if (!camera.pending) loadCameraSnapshotOnce(camera, view, state);
  return view;
}

function loadCameraSnapshotOnce(camera, view, state) {
  const image = new Image();
  image.alt = `${camera.name}打开页面时的快照`;
  image.decoding = "async";
  image.addEventListener("load", () => {
    view.classList.add("loaded");
  }, {once: true});
  image.addEventListener("error", () => {
    image.remove();
    view.classList.add("failed");
    state.textContent = "快照获取失败";
  }, {once: true});
  image.src = `/api/cameras/${encodeURIComponent(camera.id)}/snapshot`;
  view.prepend(image);
}

function updateCameraApplyState() {
  const {changedCount, unresolvedCount} = updateCameraGroupControls();

  applyCameraAudioButton.disabled = cameraSettings.applying || changedCount === 0 || unresolvedCount > 0;
  applyCameraAudioButton.classList.toggle("applying", cameraSettings.applying);
  if (cameraSettings.applying) return;

  if (unresolvedCount > 0) {
    cameraApplyStatus.textContent = `${unresolvedCount} 组录音配置不一致，请先明确选择开启或关闭。`;
    cameraApplyStatus.dataset.tone = "error";
  } else if (changedCount > 0) {
    cameraApplyStatus.textContent = `${changedCount} 组录音设置待应用，当前录像尚未改变。`;
    cameraApplyStatus.dataset.tone = "pending";
  } else {
    cameraApplyStatus.textContent = "当前录音设置已应用。";
    cameraApplyStatus.dataset.tone = "";
  }
}

function updateCameraGroupControls() {
  let changedCount = 0;
  let unresolvedCount = 0;

  for (const group of cameraSettings.groups) {
    const current = cameraSettings.currentAudio.get(group.id);
    const pending = cameraSettings.pendingAudio.get(group.id);
    const unresolved = group.hasConnectedCameras && pending === null;
    const changed = group.hasConnectedCameras && !unresolved && pending !== current;
    if (unresolved) unresolvedCount += 1;
    if (changed) changedCount += 1;

    const elements = cameraSettings.groupElements.get(group.id);
    if (!elements) continue;
    elements.card.classList.toggle("has-pending-change", changed);
    elements.card.classList.toggle("has-inconsistent-audio", unresolved);
    elements.state.classList.toggle("pending", changed);
    elements.state.classList.toggle("inconsistent", unresolved);
    elements.input.checked = pending === true;
    elements.input.indeterminate = unresolved;
    elements.input.disabled = cameraSettings.applying || !group.hasConnectedCameras;

    if (!group.hasConnectedCameras) {
      elements.state.textContent = "待接入";
      elements.audioHint.textContent = "接入摄像头后可设置";
    } else if (unresolved) {
      elements.state.textContent = "配置不一致";
      elements.audioHint.textContent = "组内设置不同，请统一选择";
    } else {
      elements.state.textContent = changed ? "待应用" : "已应用";
      elements.audioHint.textContent = "关闭可减少音频转码占用";
    }
  }

  return {changedCount, unresolvedCount};
}

async function applyCameraAudioSettings() {
  if (cameraSettings.applying || applyCameraAudioButton.disabled) return;
  const confirmed = confirm("应用录音设置会重启 Frigate，所有摄像头的录像和直播预计中断 15–60 秒。是否继续？");
  if (!confirmed) return;

  cameraSettings.applying = true;
  applyCameraAudioButton.textContent = "正在应用…";
  cameraApplyStatus.textContent = "正在保存所有录音设置并请求 Frigate 重启，请勿重复操作…";
  cameraApplyStatus.dataset.tone = "progress";
  updateCameraApplyState();

  const groups = {};
  for (const group of cameraSettings.groups) {
    const pending = cameraSettings.pendingAudio.get(group.id);
    const applied = cameraSettings.currentAudio.get(group.id);
    if (
      group.hasConnectedCameras &&
      typeof pending === "boolean" &&
      pending !== applied
    ) {
      groups[group.id] = pending;
    }
  }
  try {
    const response = await adminFetch("/api/camera-groups/record-audio", {
      method: "PUT",
      body: JSON.stringify({groups}),
    });
    const payload = await readJSON(response);
    if (!response.ok) throw new Error(payload.error || payload.message || "设置应用失败");

    const verifiedAudio = await verifyCameraAudioSettings(payload, groups);
    applyVerifiedCameraAudio(verifiedAudio);
    cameraApplyStatus.textContent = payload.restarted
      ? "Frigate 已恢复并确认录像状态。"
      : "设置一致，无需重启。";
    cameraApplyStatus.dataset.tone = "success";
    showToast(payload.restarted ? "录音设置已应用" : "录音设置无需变更");
  } catch (error) {
    cameraApplyStatus.textContent = error.message || "应用失败，录音设置尚未确认；请稍后重试。";
    cameraApplyStatus.dataset.tone = "error";
  } finally {
    cameraSettings.applying = false;
    applyCameraAudioButton.textContent = "应用并重启";
    updateCameraGroupMarkers();
  }
}

function updateCameraGroupMarkers() {
  const {changedCount, unresolvedCount} = updateCameraGroupControls();
  applyCameraAudioButton.disabled = changedCount === 0 || unresolvedCount > 0;
  applyCameraAudioButton.classList.remove("applying");
}

async function verifyCameraAudioSettings(payload, expected) {
  let states = extractCameraAudioStates(payload);
  if (!cameraAudioStatesMatch(states, expected)) {
    const response = await fetch("/api/camera-groups", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error("设置已提交，但暂时无法确认重启后的实际状态；请等待 60 秒后刷新页面。");
    }
    states = extractCameraAudioStates(await response.json());
  }

  if (!cameraAudioStatesMatch(states, expected)) {
    throw new Error("服务器返回的录音状态与提交值不一致，未将本地选择标记为已生效。");
  }
  return states;
}

function extractCameraAudioStates(payload) {
  const rawGroups = Array.isArray(payload) ? payload : payload && payload.groups;
  const states = new Map();

  if (Array.isArray(rawGroups)) {
    for (const group of normalizeCameraGroups(rawGroups)) states.set(group.id, group.recordAudio);
    return states;
  }
  if (!rawGroups || typeof rawGroups !== "object") return states;

  for (const [groupID, value] of Object.entries(rawGroups)) {
    if (typeof value === "boolean" || value === null) {
      states.set(groupID, value);
      continue;
    }
    if (value && typeof value === "object") {
      const raw = value.record_audio !== undefined
        ? value.record_audio
        : value.recordAudio !== undefined
          ? value.recordAudio
          : value.RecordAudio;
      states.set(groupID, raw === null ? null : Boolean(raw));
    }
  }
  return states;
}

function cameraAudioStatesMatch(states, expected) {
  return Object.entries(expected).every(([groupID, enabled]) => states.get(groupID) === enabled);
}

function applyVerifiedCameraAudio(states) {
  for (const group of cameraSettings.groups) {
    if (!states.has(group.id)) continue;
    const enabled = states.get(group.id);
    group.recordAudio = enabled;
    cameraSettings.currentAudio.set(group.id, enabled);
    cameraSettings.pendingAudio.set(group.id, enabled);
  }
}

async function readJSON(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

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
