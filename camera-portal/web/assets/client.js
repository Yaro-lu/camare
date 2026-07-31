const state = {
  cameras: [],
  mode: "live",
  recordingsByCamera: new Map(),
  replayPlayers: new Map(),
  replayMessages: new Map(),
  replayLoaders: new Map(),
  hlsByCamera: new Map(),
  replaySessions: new Map(),
  replayWatchdogs: new Map(),
  replayRetryCounts: new Map(),
  livePlayers: new Map(),
  liveRetryCounts: new Map(),
  playableRanges: [],
  replayGeneration: 0,
  recordingsAbortController: null,
  recordingTimezone: "Asia/Shanghai",
  recordingUTCOffsetSeconds: 8 * 60 * 60,
  focusedReplayCamera: null,
  replayAdvancePending: false,
  suspended: false,
};

const REPLAY_WINDOW_SECONDS = 60 * 60;
const LIVE_FIRST_FRAME_TIMEOUT_MS = 10000;
const LIVE_STALL_TIMEOUT_MS = 8000;
const REPLAY_FIRST_FRAME_TIMEOUT_MS = 10000;
const MAX_STREAM_RECOVERY_ATTEMPTS = 3;

const liveView = document.querySelector("#liveView");
const replayView = document.querySelector("#replayView");
const replayGrid = document.querySelector("#replayGrid");
const replayDate = document.querySelector("#replayDate");
const replayTime = document.querySelector("#replayTime");
const timeline = document.querySelector("#timeline");
const timelineTime = document.querySelector("#timelineTime");
const availability = document.querySelector("#availability");
const statusText = document.querySelector("#statusText");
const toast = document.querySelector("#toast");
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
let hlsLibraryPromise;
let jsmpegLibraryPromise;
let videoRTCModulePromise;

window.addEventListener("pagehide", () => {
  suspendCurrentPlayback();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    resumeCurrentPlayback();
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    suspendCurrentPlayback();
    return;
  }
  resumeCurrentPlayback();
});
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement) {
    restoreReplayGrid().catch((error) => handlePlaybackError(error, "双画面恢复失败"));
  }
});

start().catch((error) => {
  statusText.textContent = "连接失败";
  showToast(error.message || "视频服务暂时不可用");
});

async function start() {
  const response = await fetch("/api/config", {credentials: "same-origin"});
  if (response.status === 401) {
    location.reload();
    return;
  }
  if (!response.ok) throw new Error("无法读取摄像头");
  const payload = await response.json();
  state.cameras = payload.cameras || [];
  state.recordingTimezone = payload.recording_timezone || "Asia/Shanghai";
  const recordingOffset = Number(payload.recording_utc_offset_seconds);
  state.recordingUTCOffsetSeconds = Number.isFinite(recordingOffset) ? recordingOffset : 8 * 60 * 60;
  if (!isIOS) {
    await ensureVideoRTC();
  }
  buildLiveView();
  buildReplayView();
  setToday();
  bindControls();
  updateLiveStatus();
}

function buildLiveView() {
  if (state.mode !== "live") return;
  if (document.hidden) {
    state.suspended = true;
    return;
  }
  state.suspended = false;
  stopLive();
  liveView.replaceChildren();
  for (const camera of state.cameras) {
    createLiveCard(camera);
  }
  updateLiveStatus();
}

function createLiveCard(camera, replaceTarget = null) {
  const card = document.createElement("article");
  card.className = "camera-card";
  card.dataset.camera = camera.id;
  card.tabIndex = 0;
  card.setAttribute("aria-label", `${camera.name}，点击全屏`);

  const player = isIOS ? document.createElement("div") : document.createElement("video-rtc");
  if (isIOS) {
    player.className = "jsmpeg-stage";
  } else {
    player.mode = "mse,hls,webrtc";
    player.media = "video";
    player.background = false;
    player.visibilityCheck = false;
  }

  const loader = createStreamLoader();
  const label = document.createElement("div");
  label.className = "camera-label";
  const dot = document.createElement("i");
  const name = document.createElement("span");
  name.textContent = camera.name;
  label.append(dot, name);
  card.append(player, loader, label);
  if (replaceTarget) {
    replaceTarget.replaceWith(card);
  } else {
    liveView.append(card);
  }

  const entry = {
    card,
    player,
    video: null,
    controller: null,
    loader,
    ready: false,
    error: false,
    destroy: null,
    timeout: null,
    stableTimer: null,
    lastProgressAt: Date.now(),
    restarting: false,
    disposed: false,
    userPaused: false,
  };
  state.livePlayers.set(camera.id, entry);
  setStreamLoading(loader, true);

  if (isIOS) {
    const canvas = document.createElement("canvas");
    canvas.setAttribute("aria-label", `${camera.name}直播`);
    player.append(canvas);
    entry.controller = createJSMpegController();
    addPlaybackToggle(card, () => entry.controller);
    startJSMpegLive(camera.id, canvas, entry);
  } else {
    const video = player.video;
    entry.video = video;
    entry.controller = video;
    configureVideo(video);
    trackLiveVideo(camera.id, video, entry);
    addPlaybackToggle(card, () => entry.video, (paused) => {
      entry.userPaused = paused;
      if (paused) {
        clearTimeout(entry.timeout);
      } else {
        entry.lastProgressAt = Date.now();
        armLiveWatchdog(camera.id, entry);
      }
    });
    player.src = `/live/mse/api/ws?src=${encodeURIComponent(camera.id)}`;
    armLiveWatchdog(camera.id, entry, LIVE_FIRST_FRAME_TIMEOUT_MS);
  }

  bindFullscreen(card, player);
  return card;
}

async function ensureVideoRTC() {
  if (customElements.get("video-rtc")) return;
  if (!videoRTCModulePromise) {
    videoRTCModulePromise = import("/vendor/video-rtc.js").then(({VideoRTC}) => {
      if (!customElements.get("video-rtc")) {
        customElements.define("video-rtc", VideoRTC);
      }
    });
  }
  await videoRTCModulePromise;
}

function buildReplayView() {
  replayGrid.replaceChildren();
  state.replayPlayers.clear();
  state.replayMessages.clear();
  state.replayLoaders.clear();
  state.replaySessions.clear();
  state.focusedReplayCamera = null;
  for (const camera of state.cameras) {
    const card = document.createElement("article");
    card.className = "camera-card replay-card";
    card.dataset.camera = camera.id;
    card.tabIndex = 0;
    card.setAttribute("aria-label", `${camera.name}回看，点击全屏`);

    const video = document.createElement("video");
    configureVideo(video);
    video.preload = "metadata";
    video.hidden = true;

    const empty = document.createElement("div");
    empty.className = "stage-message";
    empty.textContent = "选择时间开始回看";

    const loader = createStreamLoader();
    const label = document.createElement("div");
    label.className = "camera-label";
    const dot = document.createElement("i");
    const name = document.createElement("span");
    name.textContent = camera.name;
    label.append(dot, name);

    card.append(video, empty, loader, label);
    replayGrid.append(card);
    trackVideoLoading(video, loader);
    addPlaybackToggle(card, () => video);
    state.replayPlayers.set(camera.id, video);
    state.replayMessages.set(camera.id, empty);
    state.replayLoaders.set(camera.id, loader);
    video.addEventListener("webkitendfullscreen", () => {
      restoreReplayGrid(camera.id).catch((error) => handlePlaybackError(error, "双画面恢复失败"));
    });
    video.addEventListener("ended", () => {
      advanceReplayAfterEnd(camera.id).catch((error) => handlePlaybackError(error, "下一段录像加载失败"));
    });
    bindFullscreen(card, video);
  }
}

function configureVideo(video) {
  if (!video) return;
  video.controls = false;
  video.autoplay = true;
  video.defaultMuted = true;
  video.muted = true;
  video.playsInline = true;
  video.setAttribute("playsinline", "");
  video.setAttribute("webkit-playsinline", "");
}

function createStreamLoader() {
  const loader = document.createElement("div");
  loader.className = "stream-loader";
  loader.setAttribute("role", "status");
  loader.setAttribute("aria-label", "视频加载中");
  loader.hidden = true;
  const ring = document.createElement("i");
  const text = document.createElement("span");
  text.textContent = "加载中";
  loader.append(ring, text);
  return loader;
}

function setStreamLoading(loader, loading) {
  if (!loader) return;
  loader.hidden = !loading;
  loader.classList.toggle("is-loading", loading);
}

function trackVideoLoading(video, loader) {
  if (!video) return;
  for (const eventName of ["loadstart", "waiting", "stalled", "seeking"]) {
    video.addEventListener(eventName, () => {
      if (eventName === "loadstart" || !video.paused) {
        setStreamLoading(loader, true);
      }
    });
  }
  for (const eventName of ["playing", "canplay", "seeked", "pause", "ended", "error", "emptied"]) {
    video.addEventListener(eventName, () => setStreamLoading(loader, false));
  }
}

function trackLiveVideo(cameraID, video, entry) {
  if (!video) return;
  trackVideoLoading(video, entry.loader);
  for (const eventName of ["playing", "canplay"]) {
    video.addEventListener(eventName, () => {
      if (state.livePlayers.get(cameraID) !== entry) return;
      entry.ready = true;
      entry.error = false;
      entry.lastProgressAt = Date.now();
      clearTimeout(entry.stableTimer);
      entry.stableTimer = setTimeout(() => {
        if (state.livePlayers.get(cameraID) === entry && entry.ready) {
          state.liveRetryCounts.delete(cameraID);
        }
      }, 30000);
      updateLiveStatus();
    });
  }
  video.addEventListener("timeupdate", () => {
    if (state.livePlayers.get(cameraID) === entry) {
      entry.lastProgressAt = Date.now();
    }
  });
  for (const eventName of ["waiting", "stalled", "loadstart", "seeking"]) {
    video.addEventListener(eventName, () => {
      if (state.livePlayers.get(cameraID) !== entry) return;
      entry.ready = false;
      updateLiveStatus();
    });
  }
  video.addEventListener("error", () => {
    if (state.livePlayers.get(cameraID) !== entry) return;
    entry.ready = false;
    entry.error = false;
    armLiveWatchdog(cameraID, entry, 1000);
    updateLiveStatus();
  });
  for (const eventName of ["pause", "emptied"]) {
    video.addEventListener(eventName, () => {
      if (state.livePlayers.get(cameraID) !== entry) return;
      entry.ready = false;
      updateLiveStatus();
    });
  }
}

function armLiveWatchdog(cameraID, entry, delay = LIVE_STALL_TIMEOUT_MS) {
  if (entry.disposed) return;
  clearTimeout(entry.timeout);
  entry.timeout = setTimeout(() => {
    if (
      state.livePlayers.get(cameraID) !== entry ||
      entry.disposed ||
      entry.restarting ||
      state.mode !== "live" ||
      state.suspended ||
      document.hidden
    ) {
      return;
    }
    if (entry.userPaused) {
      return;
    }
    const sinceProgress = Date.now() - entry.lastProgressAt;
    if (entry.ready && sinceProgress < LIVE_STALL_TIMEOUT_MS) {
      armLiveWatchdog(cameraID, entry, LIVE_STALL_TIMEOUT_MS - sinceProgress);
      return;
    }
    restartLiveCamera(cameraID, entry);
  }, Math.max(250, delay));
}

function restartLiveCamera(cameraID, entry) {
  if (
    state.livePlayers.get(cameraID) !== entry ||
    entry.restarting ||
    state.mode !== "live" ||
    state.suspended ||
    document.hidden
  ) {
    return;
  }
  const attempt = (state.liveRetryCounts.get(cameraID) || 0) + 1;
  if (attempt > MAX_STREAM_RECOVERY_ATTEMPTS) {
    entry.error = true;
    entry.ready = false;
    setStreamLoading(entry.loader, false);
    updateLiveStatus();
    return;
  }
  state.liveRetryCounts.set(cameraID, attempt);
  entry.restarting = true;
  entry.ready = false;
  entry.error = false;
  setStreamLoading(entry.loader, true);
  updateLiveStatus();
  disposeLiveEntry(entry);
  entry.timeout = setTimeout(() => {
    if (
      state.livePlayers.get(cameraID) !== entry ||
      state.mode !== "live" ||
      state.suspended ||
      document.hidden
    ) {
      return;
    }
    const camera = state.cameras.find((item) => item.id === cameraID);
    if (!camera) return;
    createLiveCard(camera, entry.card);
    updateLiveStatus();
  }, Math.min(1500, 250 * attempt));
}

function updateLiveStatus() {
  if (state.mode !== "live") return;
  const entries = [...state.livePlayers.values()];
  if (entries.some((entry) => entry.error || entry.video?.error)) {
    statusText.textContent = "部分直播异常";
  } else if (entries.length === state.cameras.length && entries.every((entry) => entry.ready)) {
    statusText.textContent = "直播中";
  } else {
    statusText.textContent = "连接中";
  }
}

function disposeLiveEntry(entry) {
  if (!entry) return;
  clearTimeout(entry.timeout);
  clearTimeout(entry.stableTimer);
  if (entry.disposed) return;
  entry.disposed = true;
  const {player, video, controller, loader} = entry;
  if (entry.destroy) {
    entry.destroy();
  } else if (controller && controller !== video) {
    controller.destroy?.();
  }
  setStreamLoading(loader, false);
  if (typeof player?.ondisconnect === "function") {
    player.ondisconnect();
  }
  if (player instanceof HTMLVideoElement) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  } else if (video instanceof HTMLVideoElement) {
    video.pause();
  }
  player?.remove();
}

function stopLive() {
  for (const entry of state.livePlayers.values()) {
    disposeLiveEntry(entry);
  }
  state.livePlayers.clear();
  state.liveRetryCounts.clear();
}

function createJSMpegController() {
  const controller = new EventTarget();
  let instance = null;
  controller.paused = false;
  controller.ended = false;
  controller.attach = (player) => {
    instance = player;
    if (controller.paused) {
      instance.pause?.();
    } else {
      instance.play?.();
    }
  };
  controller.play = () => {
    controller.paused = false;
    controller.ended = false;
    instance?.play?.();
    controller.dispatchEvent(new Event("play"));
    return Promise.resolve();
  };
  controller.pause = () => {
    controller.paused = true;
    instance?.pause?.();
    controller.dispatchEvent(new Event("pause"));
  };
  controller.markPlaying = () => {
    controller.paused = false;
    controller.dispatchEvent(new Event("playing"));
  };
  controller.destroy = () => {
    controller.paused = true;
    instance?.destroy?.();
    instance = null;
    controller.dispatchEvent(new Event("emptied"));
  };
  return controller;
}

async function startJSMpegLive(cameraID, canvas, entry) {
  const armFailureTimeout = () => {
    clearTimeout(entry.timeout);
    entry.timeout = setTimeout(() => {
      if (state.livePlayers.get(cameraID) !== entry || entry.ready) return;
      restartLiveCamera(cameraID, entry);
    }, 15000);
  };
  armFailureTimeout();

  try {
    await ensureJSMpegLibrary();
    if (state.livePlayers.get(cameraID) !== entry || entry.ready) return;
    if (state.livePlayers.get(cameraID) !== entry || state.mode !== "live") return;

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const source = `${protocol}//${location.host}/live/jsmpeg/${encodeURIComponent(cameraID)}`;
    const onFailure = () => {
      if (state.livePlayers.get(cameraID) !== entry) return;
      entry.ready = false;
      entry.error = false;
      setStreamLoading(entry.loader, true);
      updateLiveStatus();
      armFailureTimeout();
    };
    const livePlayer = new window.JSMpeg.VideoElement(
      entry.player,
      source,
      {
        canvas,
        autoplay: true,
        control: false,
        decodeFirstFrame: true,
      },
      {
        protocols: [],
        audio: false,
        pauseWhenHidden: false,
        disableGl: true,
        disableWebAssembly: true,
        videoBufferSize: 4 * 1024 * 1024,
        onVideoDecode: () => {
          if (state.livePlayers.get(cameraID) !== entry) return;
          const wasReady = entry.ready;
          clearTimeout(entry.timeout);
          entry.ready = true;
          entry.error = false;
          if (!wasReady) {
            setStreamLoading(entry.loader, false);
            entry.controller.markPlaying();
            updateLiveStatus();
          }
        },
      },
    );
    entry.controller.attach(livePlayer);

    const socket = livePlayer.player?.source?.socket;
    socket?.addEventListener("error", onFailure);
    socket?.addEventListener("close", onFailure);
    entry.destroy = () => {
      clearTimeout(entry.timeout);
      socket?.removeEventListener("error", onFailure);
      socket?.removeEventListener("close", onFailure);
      entry.controller.destroy();
    };
  } catch {
    clearTimeout(entry.timeout);
    entry.ready = false;
    entry.error = true;
    setStreamLoading(entry.loader, false);
    updateLiveStatus();
    showToast("直播播放器加载失败");
  }
}

function ensureJSMpegLibrary() {
  if (window.JSMpeg) return Promise.resolve();
  if (!jsmpegLibraryPromise) {
    jsmpegLibraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/assets/jsmpeg-player.min.js";
      script.onload = () => window.JSMpeg ? resolve() : reject(new Error("JSMPEG unavailable"));
      script.onerror = () => reject(new Error("JSMPEG load failed"));
      document.head.append(script);
    });
  }
  return jsmpegLibraryPromise;
}

function addPlaybackToggle(card, getVideo, onUserToggle = null) {
  const button = document.createElement("button");
  button.className = "player-toggle";
  button.type = "button";
  button.setAttribute("aria-label", "播放");
  button.title = "播放";

  const sync = () => {
    const video = getVideo();
    const playing = Boolean(video && !video.paused && !video.ended);
    button.classList.toggle("playing", playing);
    button.setAttribute("aria-label", playing ? "暂停" : "播放");
    button.title = playing ? "暂停" : "播放";
  };

  const video = getVideo();
  if (video) {
    for (const eventName of ["playing", "play", "pause", "ended", "emptied"]) {
      video.addEventListener(eventName, sync);
    }
  }
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const current = getVideo();
    if (!current) return;
    if (current.paused || current.ended) {
      onUserToggle?.(false);
      current.muted = true;
      current.play().catch(() => showToast("视频暂时无法播放"));
    } else {
      onUserToggle?.(true);
      current.pause();
    }
  });
  card.append(button);
  sync();
}

function bindFullscreen(card, player) {
  card.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    enterFullscreen(card, player);
  });
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      enterFullscreen(card, player);
    }
  });
}

function enterFullscreen(card, player) {
  const video = player.video || card.querySelector("video");
  const cameraID = card.dataset.camera;
  const replayFocused = state.mode === "replay" && focusReplayCamera(cameraID);
  const useFocusedFallback = () => {
    card.classList.toggle("focused");
    if (!card.classList.contains("focused") && replayFocused) {
      restoreReplayGrid(cameraID).catch((error) => handlePlaybackError(error, "双画面恢复失败"));
    }
  };
  try {
    if (video) {
      configureVideo(video);
      video.play().catch(() => {});
    }
    if (video?.webkitEnterFullscreen) {
      video.webkitEnterFullscreen();
      lockLandscape();
    } else if (card.requestFullscreen) {
      card.requestFullscreen()
        .then(lockLandscape)
        .catch(useFocusedFallback);
    } else {
      useFocusedFallback();
    }
  } catch {
    useFocusedFallback();
  }
}

function focusReplayCamera(cameraID) {
  if (
    state.mode !== "replay" ||
    !cameraID ||
    !state.replaySessions.has(cameraID)
  ) {
    return false;
  }
  state.focusedReplayCamera = cameraID;
  for (const camera of state.cameras) {
    const id = camera.id;
    if (id !== cameraID) {
      stopReplay(id);
    }
    replayGrid.querySelector(`[data-camera="${CSS.escape(id)}"]`)?.classList.toggle("replay-inactive", id !== cameraID);
  }
  return true;
}

async function restoreReplayGrid(cameraID) {
  const focusedID = state.focusedReplayCamera;
  if (!focusedID || (cameraID && cameraID !== focusedID)) return;

  const timestamp = currentReplayTimestamp(focusedID);
  state.focusedReplayCamera = null;
  for (const card of replayGrid.querySelectorAll(".camera-card")) {
    card.classList.remove("focused", "replay-inactive");
  }
  if (state.mode !== "replay" || state.suspended) return;

  if (Number.isFinite(timestamp)) {
    const dayStart = recordingDayStartEpoch(replayDate.value);
    timeline.value = Math.max(0, Math.min(86399, Math.floor(timestamp - dayStart)));
    updateTimelineLabel();
  }
  const selectedDate = replayDate.value;
  const generation = cancelReplayWork(false);
  await playSelectedTime(generation, selectedDate);
}

function lockLandscape() {
  screen.orientation?.lock?.("landscape").catch(() => {});
}

function bindControls() {
  for (const button of document.querySelectorAll(".mode-switch button")) {
    button.addEventListener("click", () => {
      switchMode(button.dataset.mode).catch((error) => handlePlaybackError(error, "模式切换失败"));
    });
  }
  replayDate.addEventListener("change", () => {
    loadRecordings().catch((error) => handlePlaybackError(error, "录像时间轴读取失败"));
  });
  replayTime.addEventListener("change", () => {
    const seconds = parseTimeInput(replayTime.value);
    if (seconds === null) return;
    timeline.value = seconds;
    updateTimelineLabel();
    playCurrentSelection().catch((error) => handlePlaybackError(error, "录像暂时无法播放"));
  });
  timeline.addEventListener("input", updateTimelineLabel);
  timeline.addEventListener("change", () => {
    playCurrentSelection().catch((error) => handlePlaybackError(error, "录像暂时无法播放"));
  });
}

async function switchMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.suspended = false;
  for (const button of document.querySelectorAll(".mode-switch button")) {
    button.classList.toggle("active", button.dataset.mode === mode);
  }
  const replay = mode === "replay";
  liveView.hidden = replay;
  replayView.hidden = !replay;
  statusText.textContent = replay ? "录像回看" : "连接中";
  if (replay) {
    cancelReplayWork();
    stopLive();
    liveView.replaceChildren();
    setToday();
    await loadRecordings();
  } else {
    cancelReplayWork();
    buildLiveView();
  }
}

function setToday() {
  const now = recordingDateParts(Date.now());
  replayDate.value = `${now.year}-${String(now.month).padStart(2, "0")}-${String(now.day).padStart(2, "0")}`;
  timeline.value = Math.max(0, now.hour * 3600 + now.minute * 60 + now.second - 15 * 60);
  updateTimelineLabel();
}

async function loadRecordings() {
  if (state.mode !== "replay" || !state.cameras.length || !replayDate.value) return;
  const selectedDate = replayDate.value;
  const generation = cancelReplayWork();
  const controller = new AbortController();
  state.recordingsAbortController = controller;
  for (const camera of state.cameras) {
    setStreamLoading(state.replayLoaders.get(camera.id), true);
    const message = state.replayMessages.get(camera.id);
    if (message) message.hidden = true;
  }
  const start = recordingDayStartEpoch(selectedDate);
  const end = start + 86400;
  let entries;
  try {
    entries = await Promise.all(state.cameras.map(async (camera) => {
      const url = new URL("/api/recordings", location.origin);
      url.searchParams.set("camera", camera.id);
      url.searchParams.set("after", start);
      url.searchParams.set("before", end);
      const response = await fetch(url, {
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (response.status === 401) {
        location.reload();
        throw new DOMException("设备授权已失效", "AbortError");
      }
      if (!response.ok) throw new Error("录像时间轴读取失败");
      return [camera.id, await response.json()];
    }));
  } finally {
    if (state.recordingsAbortController === controller) {
      state.recordingsAbortController = null;
    }
  }
  if (!isCurrentReplay(generation, selectedDate)) return;
  state.recordingsByCamera = new Map(entries);
  state.playableRanges = calculatePlayableRanges();
  renderAvailability(start);
  for (const camera of state.cameras) {
    const recordings = state.recordingsByCamera.get(camera.id) || [];
    showReplayMessage(camera.id, recordings.length ? "拖动时间轴开始回看" : "当天暂无录像");
  }
  if (entries.some(([, recordings]) => recordings.length)) {
    await playSelectedTime(generation, selectedDate);
  }
}

function renderAvailability(dayStart) {
  availability.replaceChildren();
  const origin = dayStart;
  for (const recording of state.playableRanges) {
    const segment = document.createElement("i");
    const left = Math.max(0, ((recording.start - origin) / 86400) * 100);
    const width = Math.max(0.08, ((recording.end - recording.start) / 86400) * 100);
    segment.style.left = `${left}%`;
    segment.style.width = `${Math.min(width, 100 - left)}%`;
    availability.append(segment);
  }
}

function calculatePlayableRanges() {
  let common = null;
  for (const camera of state.cameras) {
    const recordings = state.recordingsByCamera.get(camera.id) || [];
    const ranges = mergeSegments(recordings);
    if (!ranges.length) return [];
    common = common === null ? ranges : intersectRanges(common, ranges);
    if (!common.length) return [];
  }
  return common || [];
}

function mergeSegments(recordings) {
  const merged = [];
  const sorted = [...recordings].sort((left, right) => Number(left.start_time) - Number(right.start_time));
  for (const item of sorted) {
    const start = Number(item.start_time);
    const end = Number(item.end_time);
    const previous = merged[merged.length - 1];
    if (previous && start - previous.end <= 0.5) {
      previous.end = Math.max(previous.end, end);
    } else {
      merged.push({start, end});
    }
  }
  return merged;
}

function intersectRanges(left, right) {
  const result = [];
  let leftIndex = 0;
  let rightIndex = 0;
  while (leftIndex < left.length && rightIndex < right.length) {
    const start = Math.max(left[leftIndex].start, right[rightIndex].start);
    const end = Math.min(left[leftIndex].end, right[rightIndex].end);
    if (end > start) result.push({start, end});
    if (left[leftIndex].end < right[rightIndex].end) {
      leftIndex += 1;
    } else {
      rightIndex += 1;
    }
  }
  return result;
}

function updateTimelineLabel() {
  const seconds = Number(timeline.value);
  const value = formatSeconds(seconds);
  timelineTime.textContent = value.slice(0, 5);
  replayTime.value = value;
}

function playCurrentSelection() {
  const selectedDate = replayDate.value;
  const generation = cancelReplayWork(false);
  return playSelectedTime(generation, selectedDate);
}

async function playSelectedTime(generation, selectedDate) {
  if (!isCurrentReplay(generation, selectedDate)) return;
  const hasRecordings = [...state.recordingsByCamera.values()].some((recordings) => recordings.length);
  if (!hasRecordings) {
    for (const camera of state.cameras) {
      showReplayMessage(camera.id, "当天暂无录像");
    }
    return;
  }
  const dayStart = recordingDayStartEpoch(selectedDate);
  const original = dayStart + Number(timeline.value);
  const requested = nearestPlayableTime(original, state.playableRanges);
  if (requested !== null) {
    const snappedSecond = Math.max(0, Math.min(86399, Math.floor(requested - dayStart)));
    timeline.value = snappedSecond;
    updateTimelineLabel();
  }
  const dayEnd = dayStart + 86400;
  const playback = [];
  const selectedCameras = state.focusedReplayCamera
    ? state.cameras.filter((camera) => camera.id === state.focusedReplayCamera)
    : state.cameras;
  const selectedIDs = new Set(selectedCameras.map((camera) => camera.id));
  for (const camera of state.cameras) {
    if (!selectedIDs.has(camera.id)) {
      stopReplay(camera.id);
    }
  }
  for (const camera of selectedCameras) {
    const recordings = state.recordingsByCamera.get(camera.id) || [];
    const cameraTime = requested ?? nearestPlayableTime(original, mergeSegments(recordings));
    const segment = recordings.find((item) =>
      cameraTime !== null &&
      cameraTime >= Number(item.start_time) &&
      cameraTime <= Number(item.end_time)
    );
    if (!segment) {
      showReplayMessage(camera.id, recordings.length ? "附近没有可播放录像" : "当天暂无录像");
      continue;
    }
    const windowStart = Math.floor(dayStart + Math.floor((cameraTime - dayStart) / REPLAY_WINDOW_SECONDS) * REPLAY_WINDOW_SECONDS);
    const windowEnd = Math.min(dayEnd, windowStart + REPLAY_WINDOW_SECONDS);
    const windowRecordings = recordings
      .filter((item) =>
        Number(item.end_time) > windowStart &&
        Number(item.start_time) < windowEnd
      )
      .sort((left, right) => Number(left.start_time) - Number(right.start_time));
    const inpointOffset = calculateInpointOffset(windowStart, windowRecordings[0]);
    const startPosition = calculateSeekPosition(cameraTime, windowRecordings, inpointOffset);
    if (startPosition === null) {
      stopReplay(camera.id);
      showReplayMessage(camera.id, "附近没有可播放录像");
      continue;
    }
    const source = `/media/vod/${encodeURIComponent(camera.id)}/start/${windowStart}/end/${windowEnd}/master.m3u8`;
    playback.push(playHLS(camera.id, {
      source,
      windowStart,
      windowEnd,
      startPosition,
      requestedTimestamp: cameraTime,
      recordings: windowRecordings,
      inpointOffset,
    }, generation, selectedDate));
  }
  if (!playback.length) {
    return;
  }
  await Promise.allSettled(playback);
}

function nearestPlayableTime(requested, ranges) {
  if (!ranges.length) return null;
  let nearest = null;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const range of ranges) {
    if (requested >= range.start && requested < range.end) return requested;
    const candidate = requested < range.start ? range.start : Math.max(range.start, range.end - 0.25);
    const distance = Math.abs(candidate - requested);
    if (distance < nearestDistance) {
      nearest = candidate;
      nearestDistance = distance;
    }
  }
  return nearest;
}

function calculateInpointOffset(timeRangeStart, firstRecordingSegment) {
  if (
    firstRecordingSegment &&
    Number(firstRecordingSegment.start_time) < timeRangeStart &&
    Number(firstRecordingSegment.end_time) > timeRangeStart
  ) {
    return timeRangeStart - Number(firstRecordingSegment.start_time);
  }
  return 0;
}

function calculateSeekPosition(timestamp, recordings, inpointOffset = 0) {
  if (!recordings.length) return null;
  if (
    timestamp < Number(recordings[0].start_time) ||
    timestamp > Number(recordings[recordings.length - 1].end_time)
  ) {
    return null;
  }

  let seekSeconds = 0;
  for (const segment of recordings) {
    const start = Number(segment.start_time);
    const end = Number(segment.end_time);
    if (start > timestamp) break;
    if (end < timestamp) {
      seekSeconds += Math.max(0, end - start);
      continue;
    }
    seekSeconds += Math.max(0, timestamp - start);
    break;
  }
  seekSeconds -= inpointOffset;
  return seekSeconds >= 0 ? seekSeconds : null;
}

function calculateReplayTimestamp(playerTime, recordings, inpointOffset = 0) {
  let remaining = Math.max(0, Number(playerTime)) + inpointOffset;
  for (const segment of recordings) {
    const start = Number(segment.start_time);
    const duration = Math.max(0, Number(segment.end_time) - start);
    if (remaining <= duration) return start + remaining;
    remaining -= duration;
  }
  return recordings.length ? Number(recordings[recordings.length - 1].end_time) : null;
}

function currentReplayTimestamp(cameraID) {
  const video = state.replayPlayers.get(cameraID);
  const session = state.replaySessions.get(cameraID);
  if (!video || !session) return null;
  return calculateReplayTimestamp(video.currentTime, session.recordings, session.inpointOffset);
}

function nextPlayableTime(requested, ranges) {
  for (const range of ranges) {
    if (requested < range.start) return range.start;
    if (requested < range.end) return requested;
  }
  return null;
}

async function advanceReplayAfterEnd(cameraID) {
  if (
    state.mode !== "replay" ||
    state.suspended ||
    state.replayAdvancePending ||
    !state.replaySessions.has(cameraID)
  ) {
    return;
  }
  const windowEnd = Number(state.replaySessions.get(cameraID)?.windowEnd);
  if (!Number.isFinite(windowEnd)) return;
  const next = nextPlayableTime(windowEnd, state.playableRanges);
  if (next === null) return;

  const dayStart = recordingDayStartEpoch(replayDate.value);
  const dayEnd = dayStart + 86400;
  if (next >= dayEnd) return;
  timeline.value = Math.max(0, Math.min(86399, Math.floor(next - dayStart)));
  updateTimelineLabel();

  state.replayAdvancePending = true;
  try {
    const selectedDate = replayDate.value;
    const generation = cancelReplayWork(false);
    await playSelectedTime(generation, selectedDate);
  } finally {
    state.replayAdvancePending = false;
  }
}

function parseTimeInput(value) {
  if (!value) return null;
  const parts = value.split(":").map(Number);
  if (parts.some((part) => !Number.isFinite(part))) return null;
  const [hours, minutes, seconds = 0] = parts;
  return Math.max(0, Math.min(86399, hours * 3600 + minutes * 60 + seconds));
}

function formatSeconds(value) {
  const seconds = Math.max(0, Math.min(86399, Math.floor(Number(value))));
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

async function playHLS(cameraID, replay, generation, selectedDate) {
  if (!isCurrentReplay(generation, selectedDate)) return;
  const video = state.replayPlayers.get(cameraID);
  if (!video) return;
  const {
    source,
    windowStart,
    windowEnd,
    startPosition,
    requestedTimestamp,
    recordings,
    inpointOffset,
  } = replay;
  const existing = state.replaySessions.get(cameraID);
  const canReuse = existing?.source === source &&
    !video.error &&
    Boolean(state.hlsByCamera.get(cameraID) || video.getAttribute("src"));
  if (!canReuse) {
    stopReplay(cameraID, false);
  }
  state.replaySessions.set(cameraID, {
    source,
    windowStart,
    windowEnd,
    startPosition,
    requestedTimestamp,
    recordings,
    inpointOffset,
  });
  const loader = state.replayLoaders.get(cameraID);
  setStreamLoading(loader, true);
  video.hidden = false;
  const message = state.replayMessages.get(cameraID);
  if (message) message.hidden = true;

  if (canReuse) {
    seekReplayVideo(cameraID, video, startPosition, generation, selectedDate);
    return;
  }
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    const onNativeError = () => {
      if (
        state.mode !== "replay" ||
        state.suspended ||
        state.replaySessions.get(cameraID)?.source !== source
      ) {
        return;
      }
      stopReplay(cameraID);
      showReplayMessage(cameraID, "这段录像暂时无法播放");
    };
    state.replaySessions.get(cameraID).nativeErrorHandler = onNativeError;
    video.addEventListener("error", onNativeError, {once: true});
    armReplayWatchdog(cameraID, replay, generation, selectedDate);
    video.src = source;
    video.load();
    seekReplayVideo(cameraID, video, startPosition, generation, selectedDate);
    return;
  }
  try {
    await ensureHLSLibrary();
  } catch (error) {
    if (!isCurrentReplay(generation, selectedDate)) return;
    setStreamLoading(loader, false);
    showReplayMessage(cameraID, "录像播放器加载失败");
    throw error;
  }
  if (!isCurrentReplay(generation, selectedDate)) return;
  if (window.Hls?.isSupported()) {
    armReplayWatchdog(cameraID, replay, generation, selectedDate);
    const hls = new window.Hls({
      enableWorker: true,
      maxBufferLength: 10,
      maxBufferSize: 20 * 1000 * 1000,
      backBufferLength: 10,
      startPosition,
    });
    state.hlsByCamera.set(cameraID, hls);
    hls.attachMedia(video);
    hls.loadSource(source);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
      if (
        state.mode !== "replay" ||
        state.suspended ||
        state.hlsByCamera.get(cameraID) !== hls ||
        state.replaySessions.get(cameraID)?.source !== source
      ) {
        return;
      }
      video.play().catch(() => {});
    });
    hls.on(window.Hls.Events.ERROR, (_, data) => {
      if (
        data.fatal &&
        state.mode === "replay" &&
        !state.suspended &&
        state.hlsByCamera.get(cameraID) === hls &&
        state.replaySessions.get(cameraID)?.source === source
      ) {
        stopReplay(cameraID);
        showReplayMessage(cameraID, "这段录像暂时无法播放");
      }
    });
    return;
  }
  setStreamLoading(loader, false);
  showReplayMessage(cameraID, "当前浏览器不支持录像播放");
}

function seekReplayVideo(cameraID, video, startPosition, generation, selectedDate) {
  let attempts = 0;
  const apply = () => {
    if (
      !isCurrentReplay(generation, selectedDate) ||
      state.replayPlayers.get(cameraID) !== video ||
      !state.replaySessions.has(cameraID)
    ) {
      return;
    }
    if (video.seekable.length === 0 && attempts < 20) {
      attempts += 1;
      setTimeout(apply, 100);
      return;
    }
    try {
      let target = startPosition;
      if (video.seekable.length) {
        const first = video.seekable.start(0);
        const last = video.seekable.end(video.seekable.length - 1);
        target = Math.max(first, Math.min(target, Math.max(first, last - 0.05)));
      }
      if (Math.abs(video.currentTime - target) > 0.25) {
        video.currentTime = target;
      }
    } catch {
      // Metadata may still be settling; the next user seek will retry.
    }
    video.play().catch(() => {
      if (!isCurrentReplay(generation, selectedDate)) return;
      setStreamLoading(state.replayLoaders.get(cameraID), false);
      showReplayMessage(cameraID, "这段录像暂时无法播放");
    });
  };
  if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    apply();
  } else {
    video.addEventListener("loadeddata", apply, {once: true});
  }
}

function ensureHLSLibrary() {
  if (window.Hls) return Promise.resolve();
  if (!hlsLibraryPromise) {
    hlsLibraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/assets/hls.min.js";
      script.onload = resolve;
      script.onerror = () => reject(new Error("录像播放器加载失败"));
      document.head.append(script);
    });
  }
  return hlsLibraryPromise;
}

function clearReplayWatchdog(cameraID, expected = null) {
  const watchdog = state.replayWatchdogs.get(cameraID);
  if (!watchdog || (expected && watchdog !== expected)) return;
  clearTimeout(watchdog.timer);
  watchdog.video?.removeEventListener("playing", watchdog.onPlaying);
  state.replayWatchdogs.delete(cameraID);
}

function armReplayWatchdog(cameraID, replay, generation, selectedDate) {
  clearReplayWatchdog(cameraID);
  const video = state.replayPlayers.get(cameraID);
  if (!video) return;
  const watchdog = {
    video,
    timer: null,
    onPlaying: null,
  };
  watchdog.onPlaying = () => {
    if (state.replayWatchdogs.get(cameraID) !== watchdog) return;
    clearReplayWatchdog(cameraID, watchdog);
    state.replayRetryCounts.delete(cameraID);
  };
  video.addEventListener("playing", watchdog.onPlaying);
  watchdog.timer = setTimeout(() => {
    if (state.replayWatchdogs.get(cameraID) !== watchdog) return;
    clearReplayWatchdog(cameraID, watchdog);
    if (
      !isCurrentReplay(generation, selectedDate) ||
      state.replaySessions.get(cameraID)?.source !== replay.source
    ) {
      return;
    }
    const attempt = (state.replayRetryCounts.get(cameraID) || 0) + 1;
    if (attempt > MAX_STREAM_RECOVERY_ATTEMPTS) {
      state.replayRetryCounts.delete(cameraID);
      stopReplay(cameraID);
      showReplayMessage(cameraID, "这段录像暂时无法播放");
      return;
    }
    state.replayRetryCounts.set(cameraID, attempt);
    stopReplay(cameraID, false);
    setStreamLoading(state.replayLoaders.get(cameraID), true);
    setTimeout(() => {
      if (!isCurrentReplay(generation, selectedDate)) return;
      playHLS(cameraID, replay, generation, selectedDate)
        .catch((error) => handlePlaybackError(error, "录像暂时无法播放"));
    }, Math.min(1500, 250 * attempt));
  }, REPLAY_FIRST_FRAME_TIMEOUT_MS);
  state.replayWatchdogs.set(cameraID, watchdog);
}

function stopReplay(cameraID, resetRecovery = true) {
  const cameraIDs = cameraID ? [cameraID] : state.cameras.map((camera) => camera.id);
  for (const id of cameraIDs) {
    clearReplayWatchdog(id);
    if (resetRecovery) {
      state.replayRetryCounts.delete(id);
    }
    const session = state.replaySessions.get(id);
    const hls = state.hlsByCamera.get(id);
    if (hls) {
      hls.destroy();
      state.hlsByCamera.delete(id);
    }
    const video = state.replayPlayers.get(id);
    if (video && session?.nativeErrorHandler) {
      video.removeEventListener("error", session.nativeErrorHandler);
    }
    state.replaySessions.delete(id);
    if (!video) continue;
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.hidden = true;
    setStreamLoading(state.replayLoaders.get(id), false);
  }
}

function cancelReplayWork(stopPlayers = true) {
  state.replayGeneration += 1;
  state.recordingsAbortController?.abort();
  state.recordingsAbortController = null;
  if (stopPlayers) {
    stopReplay();
    state.focusedReplayCamera = null;
    for (const card of replayGrid.querySelectorAll(".camera-card")) {
      card.classList.remove("focused", "replay-inactive");
    }
  }
  return state.replayGeneration;
}

function isCurrentReplay(generation, selectedDate) {
  return state.mode === "replay" &&
    state.replayGeneration === generation &&
    replayDate.value === selectedDate &&
    !state.suspended;
}

function suspendCurrentPlayback() {
  if (state.suspended) return;
  state.suspended = true;
  if (state.mode === "live") {
    stopLive();
    liveView.replaceChildren();
  } else {
    cancelReplayWork();
  }
}

function resumeCurrentPlayback() {
  if (document.hidden || !state.suspended || !state.cameras.length) return;
  state.suspended = false;
  if (state.mode === "live") {
    buildLiveView();
  } else {
    loadRecordings().catch((error) => handlePlaybackError(error, "录像时间轴读取失败"));
  }
}

function handlePlaybackError(error, fallback) {
  if (error?.name === "AbortError") return;
  showToast(error?.message || fallback);
}

function showReplayMessage(cameraID, text) {
  const message = state.replayMessages.get(cameraID);
  if (!message) return;
  setStreamLoading(state.replayLoaders.get(cameraID), false);
  message.textContent = text;
  message.hidden = false;
}

function recordingDayStartEpoch(value) {
  const [year, month, day] = value.split("-").map(Number);
  return Math.floor(Date.UTC(year, month - 1, day) / 1000) - state.recordingUTCOffsetSeconds;
}

function recordingDateParts(timestamp) {
  const shifted = new Date(timestamp + state.recordingUTCOffsetSeconds * 1000);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
    second: shifted.getUTCSeconds(),
  };
}

let toastTimer;
function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}
