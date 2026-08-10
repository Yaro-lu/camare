import {
  DAY_SECONDS,
  TIMELINE_WINDOW_SECONDS,
  clampTimestamp,
  rebaseRollingWindow,
  splitTimeRange,
} from "./timeline-core.mjs?v=20260810-1";

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
  replayUserPaused: new Set(),
  livePlayers: new Map(),
  liveRetryCounts: new Map(),
  playableRanges: [],
  timelineRanges: [],
  replayGeneration: 0,
  recordingsAbortController: null,
  recordingsDate: "",
  recordingsWindowStart: 0,
  timelineRecordingsByCamera: new Map(),
  recordingChunkCache: new Map(),
  recordingChunkPromises: new Map(),
  timelineIndexGeneration: 0,
  timelineIndexPromise: null,
  recordingTimezone: "Asia/Shanghai",
  recordingUTCOffsetSeconds: 8 * 60 * 60,
  focusedReplayCamera: null,
  replayAdvancePending: false,
  timelinePixelsPerSecond: 0,
  timelineWindowStart: 0,
  timelineSelectedTimestamp: 0,
  timelineGesture: null,
  timelineMotionFrame: 0,
  timelineCommitTimer: 0,
  timelineSnapDirection: 0,
  timelineFollowing: true,
  timelineResizeObserver: null,
  suspended: false,
};

const REPLAY_WINDOW_SECONDS = 60 * 60;
const TIMELINE_STEP_SECONDS = 5;
const TIMELINE_HISTORY_SECONDS = 14 * DAY_SECONDS;
const TIMELINE_VISIBLE_SECONDS = 2 * 60 * 60;
const TIMELINE_SETTLE_MS = 140;
const TIMELINE_KEY_COMMIT_MS = 260;
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
const timelineViewport = document.querySelector("#timelineViewport");
const timelineTrack = document.querySelector("#timelineTrack");
const timelineRuler = document.querySelector("#timelineRuler");
const timelineLegend = document.querySelector("#timelineLegend");
const statusText = document.querySelector("#statusText");
const toast = document.querySelector("#toast");
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
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
    addPlaybackToggle(card, () => video, (paused) => {
      const session = state.replaySessions.get(camera.id);
      if (session) session.shouldPlay = !paused;
      if (paused) {
        state.replayUserPaused.add(camera.id);
        state.hlsByCamera.get(camera.id)?.stopLoad?.();
        clearReplayWatchdog(camera.id);
      } else {
        state.replayUserPaused.delete(camera.id);
        state.hlsByCamera.get(camera.id)?.startLoad?.(video.currentTime);
        if (session) {
          armReplayWatchdog(camera.id, session, state.replayGeneration, replayDate.value);
        }
      }
    });
    state.replayPlayers.set(camera.id, video);
    state.replayMessages.set(camera.id, empty);
    state.replayLoaders.set(camera.id, loader);
    video.addEventListener("webkitendfullscreen", () => {
      restoreReplayGrid(camera.id).catch((error) => handlePlaybackError(error, "双画面恢复失败"));
    });
    video.addEventListener("ended", () => {
      advanceReplayAfterEnd(camera.id).catch((error) => handlePlaybackError(error, "下一段录像加载失败"));
    });
    video.addEventListener("timeupdate", () => followReplayTime(camera.id));
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
      if (!(state.mode === "replay" && state.replayUserPaused.has(cameraID))) {
        video.play().catch(() => {});
      }
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
    setTimelineTimestamp(timestamp, {rebase: false});
  }
  const selectedDate = preparePlaybackRecordings(state.timelineSelectedTimestamp);
  const generation = cancelReplayWork(false, false);
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
    cancelTimelineMotion();
    const seconds = parseTimeInput(replayTime.value) ?? 0;
    const dayStart = recordingDayStartEpoch(replayDate.value);
    state.timelineWindowStart = dayStart - DAY_SECONDS;
    state.timelineSelectedTimestamp = dayStart + seconds;
    updateTimelineBounds();
    buildTimelineRuler();
    setTimelineTimestamp(state.timelineSelectedTimestamp, {rebase: false});
    loadRecordings().catch((error) => handlePlaybackError(error, "录像时间轴读取失败"));
  });
  replayTime.addEventListener("change", () => {
    const seconds = parseTimeInput(replayTime.value);
    if (seconds === null) return;
    cancelTimelineMotion();
    state.timelineSnapDirection = 0;
    setTimelineTimestamp(recordingDayStartEpoch(replayDate.value) + seconds);
    if (state.recordingsAbortController) return;
    commitTimelineSelection();
  });
  buildTimelineRuler();
  buildTimelineLegend();
  bindTimelinePointerControls();
  bindTimelineKeyboardControls();
  if ("ResizeObserver" in window) {
    state.timelineResizeObserver = new ResizeObserver(() => layoutTimeline());
    state.timelineResizeObserver.observe(timelineViewport);
  } else {
    window.addEventListener("resize", layoutTimeline);
  }
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
    state.replayUserPaused.clear();
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
  const nowTimestamp = Math.floor(Date.now() / 1000);
  const now = recordingDateParts(nowTimestamp * 1000);
  const today = formatRecordingDate(now);
  replayDate.max = today;
  replayDate.min = formatRecordingDate(recordingDateParts((nowTimestamp - TIMELINE_HISTORY_SECONDS) * 1000));
  replayDate.value = today;
  state.timelineWindowStart = recordingDayStartEpoch(today) - DAY_SECONDS;
  state.timelineSelectedTimestamp = nowTimestamp - 15 * 60;
  updateTimelineBounds();
  buildTimelineRuler();
  setTimelineTimestamp(state.timelineSelectedTimestamp, {rebase: false});
}

async function loadRecordings() {
  if (state.mode !== "replay" || !state.cameras.length || !replayDate.value) return;
  const selectedDate = formatRecordingDate(recordingDateParts(state.timelineSelectedTimestamp * 1000));
  const generation = cancelReplayWork();
  state.timelineFollowing = true;
  state.timelineSnapDirection = 0;
  state.recordingsWindowStart = 0;
  state.recordingsByCamera = new Map();
  state.timelineRecordingsByCamera = new Map();
  state.recordingsDate = "";
  state.playableRanges = [];
  state.timelineRanges = [];
  availability.replaceChildren();
  for (const camera of state.cameras) {
    setStreamLoading(state.replayLoaders.get(camera.id), true);
    const message = state.replayMessages.get(camera.id);
    if (message) message.hidden = true;
  }
  state.timelineIndexPromise = loadRecordingWindow(state.timelineWindowStart, {blocking: true});
  const loaded = await state.timelineIndexPromise;
  if (!loaded || !isCurrentReplay(generation, selectedDate)) return;
  preparePlaybackRecordings(state.timelineSelectedTimestamp);
  for (const camera of state.cameras) {
    const recordings = state.timelineRecordingsByCamera.get(camera.id) || [];
    showReplayMessage(camera.id, recordings.length ? "拖动时间轴开始回看" : "这48小时暂无录像");
  }
  if ([...state.recordingsByCamera.values()].some((recordings) => recordings.length)) {
    await playSelectedTime(generation, selectedDate);
  }
}

function preparePlaybackRecordings(timestamp) {
  const selectedDate = formatRecordingDate(recordingDateParts(timestamp * 1000));
  const dayStart = recordingDayStartEpoch(selectedDate);
  const dayEnd = dayStart + DAY_SECONDS;
  const entries = state.cameras.map((camera) => {
    const recordings = (state.timelineRecordingsByCamera.get(camera.id) || [])
      .filter((item) => Number(item.end_time) > dayStart && Number(item.start_time) < dayEnd);
    return [camera.id, recordings];
  });
  state.recordingsByCamera = new Map(entries);
  state.playableRanges = calculatePlayableRanges();
  state.recordingsDate = selectedDate;
  return selectedDate;
}

async function loadRecordingWindow(windowStart, {blocking = false} = {}) {
  const start = Math.floor(windowStart);
  const end = Math.min(start + TIMELINE_WINDOW_SECONDS, Math.floor(Date.now() / 1000) + 1);
  const chunks = splitTimeRange(start, end);
  const indexGeneration = ++state.timelineIndexGeneration;
  const controller = blocking ? new AbortController() : null;
  if (controller) {
    state.recordingsAbortController?.abort();
    state.recordingsAbortController = controller;
    timelineViewport.classList.add("is-disabled");
    timelineViewport.setAttribute("aria-disabled", "true");
  }

  try {
    const entries = await Promise.all(state.cameras.map(async (camera) => {
      const recordings = (await Promise.all(chunks.map((chunk) =>
        fetchRecordingChunk(camera.id, chunk, controller?.signal)
      ))).flat();
      return [camera.id, recordings];
    }));
    if (
      state.mode !== "replay" ||
      state.suspended ||
      state.timelineWindowStart !== start ||
      state.timelineIndexGeneration !== indexGeneration
    ) {
      return false;
    }
    state.timelineRecordingsByCamera = new Map(entries);
    state.timelineRanges = calculateTimelineRanges();
    state.recordingsWindowStart = start;
    renderAvailability(start);
    buildTimelineRuler();
    layoutTimeline();
    pruneRecordingChunkCache(start);
    return true;
  } finally {
    if (state.recordingsAbortController === controller) {
      state.recordingsAbortController = null;
      timelineViewport.classList.remove("is-disabled");
      timelineViewport.setAttribute("aria-disabled", "false");
    }
  }
}

async function fetchRecordingChunk(cameraID, chunk, signal) {
  const key = `${cameraID}:${chunk.start}:${chunk.end}`;
  const cached = state.recordingChunkCache.get(key);
  if (cached) return cached.recordings;
  const pending = state.recordingChunkPromises.get(key);
  if (pending) return pending;

  const request = (async () => {
    const url = new URL("/api/recordings", location.origin);
    url.searchParams.set("camera", cameraID);
    url.searchParams.set("after", chunk.start);
    url.searchParams.set("before", chunk.end);
    const response = await fetch(url, {
      credentials: "same-origin",
      signal,
    });
    if (response.status === 401) {
      location.reload();
      throw new DOMException("设备授权已失效", "AbortError");
    }
    if (!response.ok) throw new Error("录像时间轴读取失败");
    const recordings = await response.json();
    state.recordingChunkCache.set(key, {...chunk, recordings});
    return recordings;
  })();
  state.recordingChunkPromises.set(key, request);
  try {
    return await request;
  } finally {
    if (state.recordingChunkPromises.get(key) === request) {
      state.recordingChunkPromises.delete(key);
    }
  }
}

function pruneRecordingChunkCache(windowStart) {
  const keepStart = windowStart - DAY_SECONDS;
  const keepEnd = windowStart + TIMELINE_WINDOW_SECONDS + DAY_SECONDS;
  for (const [key, chunk] of state.recordingChunkCache) {
    if (chunk.end <= keepStart || chunk.start >= keepEnd) {
      state.recordingChunkCache.delete(key);
    }
  }
}

function scheduleRecordingWindowLoad() {
  const windowStart = state.timelineWindowStart;
  state.timelineIndexPromise = loadRecordingWindow(windowStart)
    .catch((error) => {
      handlePlaybackError(error, "录像时间轴读取失败");
      return false;
    });
}

async function ensureRecordingWindowLoaded() {
  if (state.recordingsWindowStart === state.timelineWindowStart) return true;
  if (state.timelineIndexPromise) await state.timelineIndexPromise;
  if (state.recordingsWindowStart === state.timelineWindowStart) return true;
  state.timelineIndexPromise = loadRecordingWindow(state.timelineWindowStart);
  return state.timelineIndexPromise;
}

function renderAvailability(windowStart = state.timelineWindowStart) {
  availability.replaceChildren();
  for (const camera of state.cameras) {
    const lane = document.createElement("div");
    lane.className = "availability-lane";
    lane.setAttribute("aria-label", `${camera.name}录像`);
    const recordings = mergeSegments(state.timelineRecordingsByCamera.get(camera.id) || []);
    for (const recording of recordings) {
      const segmentStart = Math.max(windowStart, recording.start);
      const segmentEnd = Math.min(windowStart + TIMELINE_WINDOW_SECONDS, recording.end);
      if (segmentEnd <= segmentStart) continue;
      const segment = document.createElement("i");
      const left = ((segmentStart - windowStart) / TIMELINE_WINDOW_SECONDS) * 100;
      const width = Math.max(0.02, ((segmentEnd - segmentStart) / TIMELINE_WINDOW_SECONDS) * 100);
      segment.style.left = `${left}%`;
      segment.style.width = `${Math.min(width, 100 - left)}%`;
      lane.append(segment);
    }
    availability.append(lane);
  }
}

function calculateTimelineRanges(recordingsByCamera = state.timelineRecordingsByCamera) {
  const recordings = [];
  for (const camera of state.cameras) {
    recordings.push(...(recordingsByCamera.get(camera.id) || []));
  }
  return mergeSegments(recordings, 0);
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

function mergeSegments(recordings, maximumGap = 0.5) {
  const merged = [];
  const sorted = [...recordings].sort((left, right) => Number(left.start_time) - Number(right.start_time));
  for (const item of sorted) {
    const start = Number(item.start_time);
    const end = Number(item.end_time);
    const previous = merged[merged.length - 1];
    if (previous && start - previous.end <= maximumGap) {
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

function buildTimelineRuler() {
  timelineRuler.replaceChildren();
  const windowStart = state.timelineWindowStart;
  for (let seconds = 0; seconds <= TIMELINE_WINDOW_SECONDS; seconds += 5 * 60) {
    const tick = document.createElement("i");
    tick.style.left = `${(seconds / TIMELINE_WINDOW_SECONDS) * 100}%`;
    if (seconds % (60 * 60) === 0) {
      tick.className = "hour";
    } else if (seconds % (30 * 60) === 0) {
      tick.className = "major";
    }
    if (seconds % (30 * 60) === 0 && seconds < TIMELINE_WINDOW_SECONDS && windowStart) {
      const label = document.createElement("span");
      label.textContent = formatTimelineRulerLabel(windowStart + seconds);
      tick.append(label);
    }
    timelineRuler.append(tick);
  }
}

function buildTimelineLegend() {
  timelineLegend.replaceChildren();
  for (const camera of state.cameras) {
    const item = document.createElement("span");
    const marker = document.createElement("i");
    const label = document.createElement("b");
    label.textContent = camera.name;
    item.append(marker, label);
    timelineLegend.append(item);
  }
}

function updateTimelineBounds() {
  const now = Math.floor(Date.now() / 1000);
  const earliest = now - TIMELINE_HISTORY_SECONDS;
  const minimum = Math.max(0, Math.ceil((earliest - state.timelineWindowStart) / TIMELINE_STEP_SECONDS) * TIMELINE_STEP_SECONDS);
  const maximum = Math.max(minimum, Math.min(
    TIMELINE_WINDOW_SECONDS - TIMELINE_STEP_SECONDS,
    Math.floor((now - state.timelineWindowStart) / TIMELINE_STEP_SECONDS) * TIMELINE_STEP_SECONDS,
  ));
  timeline.min = String(minimum);
  timeline.step = String(TIMELINE_STEP_SECONDS);
  timeline.max = String(maximum);
  timelineViewport.setAttribute("aria-valuemin", String(minimum));
  timelineViewport.setAttribute("aria-valuemax", String(maximum));
}

function setTimelineTimestamp(value, {syncFields = true, rebase = true} = {}) {
  const now = Math.floor(Date.now() / 1000);
  const earliest = now - TIMELINE_HISTORY_SECONDS;
  const raw = clampTimestamp(Number(value) || earliest, earliest, now);
  const timestamp = clampTimestamp(
    Math.round(raw / TIMELINE_STEP_SECONDS) * TIMELINE_STEP_SECONDS,
    earliest,
    now,
  );
  if (rebase) {
    const limits = timelineWindowLimits(now);
    const nextWindowStart = rebaseRollingWindow({
      windowStart: state.timelineWindowStart,
      selectedTimestamp: timestamp,
      ...limits,
    });
    if (nextWindowStart !== state.timelineWindowStart) {
      state.timelineWindowStart = nextWindowStart;
      updateTimelineBounds();
      buildTimelineRuler();
      renderAvailability();
      if (state.mode === "replay") scheduleRecordingWindowLoad();
    }
  }
  state.timelineSelectedTimestamp = timestamp;
  updateTimelineBounds();
  const seconds = timestamp - state.timelineWindowStart;
  timeline.value = String(seconds);
  updateTimelineLabel(syncFields);
  renderTimelinePosition(seconds);
}

function timelineWindowLimits(nowTimestamp = Math.floor(Date.now() / 1000)) {
  const earliestParts = recordingDateParts((nowTimestamp - TIMELINE_HISTORY_SECONDS) * 1000);
  const todayParts = recordingDateParts(nowTimestamp * 1000);
  return {
    minWindowStart: recordingDayStartEpoch(formatRecordingDate(earliestParts)),
    maxWindowStart: recordingDayStartEpoch(formatRecordingDate(todayParts)) - DAY_SECONDS,
  };
}

function updateTimelineLabel(syncFields = true) {
  const timestamp = state.timelineSelectedTimestamp;
  const value = formatTimestampTime(timestamp);
  timelineTime.textContent = formatTimelineCursorLabel(timestamp);
  if (syncFields) {
    replayDate.value = formatRecordingDate(recordingDateParts(timestamp * 1000));
  }
  if (syncFields && document.activeElement !== replayTime) {
    replayTime.value = value;
  }
  timelineViewport.setAttribute("aria-valuenow", String(Math.floor(timestamp - state.timelineWindowStart)));
  timelineViewport.setAttribute("aria-valuetext", formatTimelineAriaLabel(timestamp));
}

function layoutTimeline() {
  const width = timelineViewport.getBoundingClientRect().width;
  if (!width) return;
  state.timelinePixelsPerSecond = width / TIMELINE_VISIBLE_SECONDS;
  timelineTrack.style.width = `${Math.ceil(TIMELINE_WINDOW_SECONDS * state.timelinePixelsPerSecond)}px`;
  renderTimelinePosition(state.timelineSelectedTimestamp - state.timelineWindowStart);
}

function renderTimelinePosition(seconds) {
  if (!state.timelinePixelsPerSecond) return;
  timelineTrack.style.transform = `translate3d(${-seconds * state.timelinePixelsPerSecond}px, 0, 0)`;
}

function pauseReplayForTimeline() {
  const resumeCameraIDs = [];
  for (const [cameraID, video] of state.replayPlayers) {
    const session = state.replaySessions.get(cameraID);
    if (!session) continue;
    state.hlsByCamera.get(cameraID)?.stopLoad?.();
    if (!video.paused) resumeCameraIDs.push(cameraID);
    session.shouldPlay = false;
    clearReplayWatchdog(cameraID);
    video.pause();
  }
  return resumeCameraIDs;
}

function resumeReplayAfterCancelledTimeline(resumeCameraIDs) {
  for (const cameraID of resumeCameraIDs || []) {
    const video = state.replayPlayers.get(cameraID);
    const session = state.replaySessions.get(cameraID);
    if (video && session) {
      session.shouldPlay = true;
      state.hlsByCamera.get(cameraID)?.startLoad?.(video.currentTime);
      armReplayWatchdog(cameraID, session, state.replayGeneration, replayDate.value);
      video.play().catch(() => {});
    }
  }
  state.timelineFollowing = true;
}

function bindTimelinePointerControls() {
  timelineViewport.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.button !== 0 || state.recordingsAbortController) return;
    cancelTimelineMotion();
    const now = performance.now();
    const timestamp = state.timelineSelectedTimestamp;
    state.timelineFollowing = false;
    state.timelineGesture = {
      pointerID: event.pointerId,
      pointerType: event.pointerType,
      startX: event.clientX,
      startTimestamp: timestamp,
      lastTimestamp: timestamp,
      lastTime: now,
      velocity: 0,
      moved: false,
      resumeCameraIDs: pauseReplayForTimeline(),
    };
    timelineViewport.classList.add("is-scrubbing");
    try {
      timelineViewport.setPointerCapture?.(event.pointerId);
    } catch {
      // Synthetic events and older WebKit builds may reject pointer capture.
    }
    event.preventDefault();
  });

  timelineViewport.addEventListener("pointermove", (event) => {
    const gesture = state.timelineGesture;
    if (!gesture || gesture.pointerID !== event.pointerId || !state.timelinePixelsPerSecond) return;
    const deltaX = event.clientX - gesture.startX;
    const target = gesture.startTimestamp - deltaX / state.timelinePixelsPerSecond;
    const previous = state.timelineSelectedTimestamp;
    setTimelineTimestamp(target);
    const current = state.timelineSelectedTimestamp;
    const now = performance.now();
    const elapsed = Math.max(1, now - gesture.lastTime);
    const instantaneous = (current - gesture.lastTimestamp) / elapsed;
    gesture.velocity = gesture.velocity * 0.65 + instantaneous * 0.35;
    gesture.lastTimestamp = current;
    gesture.lastTime = now;
    gesture.moved ||= Math.abs(deltaX) >= 3;
    if (current !== previous) {
      state.timelineSnapDirection = Math.sign(current - previous);
    }
    event.preventDefault();
  });

  const finish = (event, cancelled = false) => {
    const gesture = state.timelineGesture;
    if (!gesture || gesture.pointerID !== event.pointerId) return;
    state.timelineGesture = null;
    timelineViewport.classList.remove("is-scrubbing");
    try {
      timelineViewport.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture can already be released when the browser cancels a gesture.
    }
    if (cancelled) {
      setTimelineTimestamp(gesture.startTimestamp);
      resumeReplayAfterCancelledTimeline(gesture.resumeCameraIDs);
      return;
    }
    if (!gesture.moved && state.timelinePixelsPerSecond) {
      const bounds = timelineViewport.getBoundingClientRect();
      const offset = event.clientX - (bounds.left + bounds.width / 2);
      const target = gesture.startTimestamp + offset / state.timelinePixelsPerSecond;
      const previous = state.timelineSelectedTimestamp;
      setTimelineTimestamp(target);
      state.timelineSnapDirection = Math.sign(state.timelineSelectedTimestamp - previous);
    }
    const useInertia = !prefersReducedMotion &&
      gesture.pointerType !== "mouse" &&
      gesture.moved &&
      Math.abs(gesture.velocity) >= 0.15;
    if (useInertia) {
      startTimelineInertia(gesture.velocity);
    } else {
      settleTimelineSelection();
    }
  };
  timelineViewport.addEventListener("pointerup", (event) => finish(event));
  timelineViewport.addEventListener("pointercancel", (event) => finish(event, true));
  timelineViewport.addEventListener("lostpointercapture", (event) => finish(event, true));
}

function bindTimelineKeyboardControls() {
  timelineViewport.addEventListener("keydown", (event) => {
    if (state.recordingsAbortController) return;
    let target = state.timelineSelectedTimestamp;
    let handled = true;
    const directionStep = event.shiftKey ? 60 : 10;
    if (event.key === "ArrowLeft") {
      target -= directionStep;
    } else if (event.key === "ArrowRight") {
      target += directionStep;
    } else if (event.key === "PageUp") {
      target += 15 * 60;
    } else if (event.key === "PageDown") {
      target -= 15 * 60;
    } else if (event.key === "Home" && state.timelineRanges.length) {
      target = state.timelineRanges[0].start;
    } else if (event.key === "End" && state.timelineRanges.length) {
      const last = state.timelineRanges[state.timelineRanges.length - 1];
      target = last.end - TIMELINE_STEP_SECONDS;
    } else {
      handled = false;
    }
    if (!handled) return;
    event.preventDefault();
    cancelTimelineMotion();
    state.timelineFollowing = false;
    pauseReplayForTimeline();
    const previous = state.timelineSelectedTimestamp;
    setTimelineTimestamp(target);
    state.timelineSnapDirection = Math.sign(state.timelineSelectedTimestamp - previous);
    state.timelineCommitTimer = window.setTimeout(settleTimelineSelection, TIMELINE_KEY_COMMIT_MS);
  });
}

function startTimelineInertia(initialVelocity) {
  let velocity = Math.max(-8, Math.min(8, initialVelocity));
  let previousTime = performance.now();
  const move = (now) => {
    const elapsed = Math.min(32, Math.max(1, now - previousTime));
    previousTime = now;
    const before = state.timelineSelectedTimestamp;
    setTimelineTimestamp(before + velocity * elapsed);
    const after = state.timelineSelectedTimestamp;
    if (after !== before) {
      state.timelineSnapDirection = Math.sign(after - before);
    }
    velocity *= Math.exp(-elapsed / 260);
    if (Math.abs(velocity) < 0.02 || after === before) {
      state.timelineMotionFrame = 0;
      settleTimelineSelection();
      return;
    }
    state.timelineMotionFrame = requestAnimationFrame(move);
  };
  state.timelineMotionFrame = requestAnimationFrame(move);
}

function settleTimelineSelection() {
  cancelTimelineMotion();
  const selected = state.timelineSelectedTimestamp;
  settleTimelineSelectionAfterIndex(selected)
    .catch((error) => handlePlaybackError(error, "录像时间轴读取失败"));
}

async function settleTimelineSelectionAfterIndex(selected) {
  const loaded = await ensureRecordingWindowLoaded();
  if (!loaded || state.timelineGesture || state.timelineSelectedTimestamp !== selected) return;
  const snapped = nearestPlayableTime(selected, state.timelineRanges, state.timelineSnapDirection);
  if (snapped === null) {
    commitTimelineSelection();
    return;
  }
  if (Math.abs(snapped - state.timelineSelectedTimestamp) < 0.5) {
    commitTimelineSelection();
    return;
  }
  animateTimelineTo(snapped, commitTimelineSelection);
}

function animateTimelineTo(target, onComplete) {
  if (prefersReducedMotion) {
    setTimelineTimestamp(target, {rebase: false});
    onComplete?.();
    return;
  }
  const start = state.timelineSelectedTimestamp;
  const startedAt = performance.now();
  const move = (now) => {
    const progress = Math.min(1, (now - startedAt) / TIMELINE_SETTLE_MS);
    const eased = 1 - Math.pow(1 - progress, 3);
    setTimelineTimestamp(start + (target - start) * eased, {rebase: false});
    if (progress >= 1) {
      state.timelineMotionFrame = 0;
      setTimelineTimestamp(target, {rebase: false});
      onComplete?.();
      return;
    }
    state.timelineMotionFrame = requestAnimationFrame(move);
  };
  state.timelineMotionFrame = requestAnimationFrame(move);
}

function cancelTimelineMotion() {
  if (state.timelineMotionFrame) {
    cancelAnimationFrame(state.timelineMotionFrame);
    state.timelineMotionFrame = 0;
  }
  clearTimeout(state.timelineCommitTimer);
  state.timelineCommitTimer = 0;
}

function commitTimelineSelection() {
  state.timelineFollowing = true;
  const work = playCurrentSelection();
  state.timelineSnapDirection = 0;
  work.catch((error) => handlePlaybackError(error, "录像暂时无法播放"));
}

function followReplayTime(cameraID) {
  if (
    state.mode !== "replay" ||
    state.suspended ||
    !state.timelineFollowing ||
    state.timelineGesture ||
    state.timelineMotionFrame ||
    document.activeElement === replayTime
  ) {
    return;
  }
  const masterCameraID = replayMasterCameraID();
  if (!masterCameraID || cameraID !== masterCameraID) return;
  const timestamp = currentReplayTimestamp(cameraID);
  if (!Number.isFinite(timestamp)) return;
  setTimelineTimestamp(timestamp);
}

function replayMasterCameraID() {
  if (state.focusedReplayCamera && state.replaySessions.has(state.focusedReplayCamera)) {
    return state.focusedReplayCamera;
  }
  return state.cameras.find((camera) => {
    const video = state.replayPlayers.get(camera.id);
    return state.replaySessions.has(camera.id) && video && !video.paused && !video.ended;
  })?.id || state.cameras.find((camera) => {
    const video = state.replayPlayers.get(camera.id);
    return state.replaySessions.has(camera.id) && video && !video.ended;
  })?.id || null;
}

function playCurrentSelection() {
  const selectedDate = preparePlaybackRecordings(state.timelineSelectedTimestamp);
  const generation = cancelReplayWork(false, false);
  return playSelectedTime(generation, selectedDate);
}

async function playSelectedTime(generation, selectedDate) {
  if (!isCurrentReplay(generation, selectedDate) || state.recordingsDate !== selectedDate) return;
  const hasRecordings = [...state.recordingsByCamera.values()].some((recordings) => recordings.length);
  if (!hasRecordings) {
    for (const camera of state.cameras) {
      showReplayMessage(camera.id, "当天暂无录像");
    }
    return;
  }
  const dayStart = recordingDayStartEpoch(selectedDate);
  const original = state.timelineSelectedTimestamp;
  const commonRequested = nearestPlayableTime(original, state.playableRanges);
  const requested = nearestPlayableTime(original, calculateTimelineRanges(state.recordingsByCamera)) ?? commonRequested;
  if (requested !== null) {
    setTimelineTimestamp(requested, {rebase: false});
  }
  const dayEnd = dayStart + DAY_SECONDS;
  const playback = [];
  const desiredPlayback = new Map();
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
    const cameraTime = requested;
    const segment = recordings.find((item) =>
      cameraTime !== null &&
      cameraTime >= Number(item.start_time) &&
      cameraTime <= Number(item.end_time)
    );
    if (!segment) {
      stopReplay(camera.id);
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
    desiredPlayback.set(camera.id, !state.replayUserPaused.has(camera.id));
    playback.push(playHLS(camera.id, {
      source,
      windowStart,
      windowEnd,
      startPosition,
      requestedTimestamp: cameraTime,
      recordings: windowRecordings,
      inpointOffset,
      shouldPlay: false,
    }, generation, selectedDate));
  }
  if (!playback.length) {
    return;
  }
  await Promise.allSettled(playback);
  await startPreparedReplay(desiredPlayback, generation, selectedDate);
}

async function startPreparedReplay(desiredPlayback, generation, selectedDate) {
  const cameraIDs = [...desiredPlayback.keys()].filter((cameraID) => state.replaySessions.has(cameraID));
  await Promise.all(cameraIDs.map((cameraID) => waitForReplayReady(cameraID, generation, selectedDate)));
  if (!isCurrentReplay(generation, selectedDate)) return;

  for (const cameraID of cameraIDs) {
    const session = state.replaySessions.get(cameraID);
    const video = state.replayPlayers.get(cameraID);
    if (!session || !video) continue;
    // The preparation barrier can take a moment on mobile Safari. Respect the
    // user's latest pause/resume choice instead of the state captured before it.
    session.shouldPlay = !state.replayUserPaused.has(cameraID);
    if (!session.shouldPlay) {
      video.pause();
      continue;
    }
    state.hlsByCamera.get(cameraID)?.startLoad?.(video.currentTime);
    armReplayWatchdog(cameraID, session, generation, selectedDate);
  }
  for (const cameraID of cameraIDs) {
    const session = state.replaySessions.get(cameraID);
    const video = state.replayPlayers.get(cameraID);
    if (!session?.shouldPlay || !video) continue;
    video.play().catch(() => {});
  }
}

function waitForReplayReady(cameraID, generation, selectedDate) {
  const video = state.replayPlayers.get(cameraID);
  const initialSession = state.replaySessions.get(cameraID);
  const source = initialSession?.source;
  const target = Number(initialSession?.startPosition);
  const nativeHLS = initialSession?.nativeHLS === true;
  const atTarget = () => video && (nativeHLS
    ? video.readyState >= HTMLMediaElement.HAVE_METADATA
    : video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
      !video.seeking &&
      (!Number.isFinite(target) || Math.abs(video.currentTime - target) <= 0.75));
  if (!video || !source || atTarget()) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    let timer;
    const finish = () => {
      clearTimeout(timer);
      video.removeEventListener("loadedmetadata", check);
      video.removeEventListener("loadeddata", check);
      video.removeEventListener("canplay", check);
      video.removeEventListener("seeked", check);
      video.removeEventListener("error", finish);
      resolve();
    };
    const check = () => {
      if (
        !isCurrentReplay(generation, selectedDate) ||
        state.replaySessions.get(cameraID)?.source !== source
      ) {
        finish();
        return;
      }
      if (atTarget()) finish();
    };
    video.addEventListener("loadedmetadata", check);
    video.addEventListener("loadeddata", check);
    video.addEventListener("canplay", check);
    video.addEventListener("seeked", check);
    video.addEventListener("error", finish, {once: true});
    timer = setTimeout(finish, 3000);
  });
}

function nearestPlayableTime(requested, ranges, direction = state.timelineSnapDirection) {
  if (!ranges.length) return null;
  if (direction > 0) {
    for (const range of ranges) {
      if (requested >= range.start && requested < range.end) return requested;
      if (requested < range.start) return range.start;
    }
  } else if (direction < 0) {
    for (let index = ranges.length - 1; index >= 0; index -= 1) {
      const range = ranges[index];
      if (requested >= range.start && requested < range.end) return requested;
      if (requested >= range.end) return Math.max(range.start, range.end - 0.25);
    }
  }
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
  const visibleCameraIDs = state.focusedReplayCamera
    ? [state.focusedReplayCamera]
    : state.cameras.map((camera) => camera.id);
  const activeSessions = visibleCameraIDs
    .map((id) => ({id, session: state.replaySessions.get(id), video: state.replayPlayers.get(id)}))
    .filter(({session}) => Boolean(session));
  if (activeSessions.some(({id, video}) => id !== cameraID && video && !video.ended)) {
    stopReplay(cameraID);
    showReplayMessage(cameraID, "此后暂无录像");
    return;
  }
  const windowEnd = Math.max(...activeSessions.map(({session}) => Number(session.windowEnd)));
  if (!Number.isFinite(windowEnd)) return;
  const nextRanges = state.focusedReplayCamera
    ? mergeSegments(state.timelineRecordingsByCamera.get(state.focusedReplayCamera) || [])
    : state.timelineRanges;
  const next = nextPlayableTime(windowEnd, nextRanges);
  const finishPlayback = () => {
    for (const {id} of activeSessions) {
      stopReplay(id);
      showReplayMessage(id, "此后暂无录像");
    }
  };
  if (next === null) {
    finishPlayback();
    return;
  }

  setTimelineTimestamp(next);

  state.replayAdvancePending = true;
  try {
    const loaded = await ensureRecordingWindowLoaded();
    if (!loaded) return;
    const selectedDate = preparePlaybackRecordings(next);
    const generation = cancelReplayWork(false, false);
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
  const total = Math.max(0, Math.min(DAY_SECONDS - 1, hours * 3600 + minutes * 60 + seconds));
  return Math.round(total / TIMELINE_STEP_SECONDS) * TIMELINE_STEP_SECONDS;
}

function formatRecordingDate(parts) {
  return `${parts.year}-${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")}`;
}

function formatTimestampTime(timestamp) {
  const parts = recordingDateParts(timestamp * 1000);
  return `${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}:${String(parts.second).padStart(2, "0")}`;
}

function formatTimelineCursorLabel(timestamp) {
  const parts = recordingDateParts(timestamp * 1000);
  return `${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")} ${formatTimestampTime(timestamp)}`;
}

function formatTimelineRulerLabel(timestamp) {
  const parts = recordingDateParts(timestamp * 1000);
  return `${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")} ${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}`;
}

function formatTimelineAriaLabel(timestamp) {
  const parts = recordingDateParts(timestamp * 1000);
  return `${formatRecordingDate(parts)} ${formatTimestampTime(timestamp)}`;
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
    shouldPlay,
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
    shouldPlay,
    nativeHLS: false,
  });
  const loader = state.replayLoaders.get(cameraID);
  setStreamLoading(loader, true);
  video.hidden = false;
  video.autoplay = shouldPlay;
  if (!shouldPlay) video.pause();
  const message = state.replayMessages.get(cameraID);
  if (message) message.hidden = true;

  if (canReuse) {
    state.hlsByCamera.get(cameraID)?.startLoad?.(startPosition);
    seekReplayVideo(cameraID, video, startPosition, generation, selectedDate);
    return;
  }
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    state.replaySessions.get(cameraID).nativeHLS = true;
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
    if (shouldPlay) {
      armReplayWatchdog(cameraID, replay, generation, selectedDate);
    }
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
    if (shouldPlay) {
      armReplayWatchdog(cameraID, replay, generation, selectedDate);
    }
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
      if (state.replaySessions.get(cameraID)?.shouldPlay !== false) {
        video.play().catch(() => {});
      } else {
        video.pause();
      }
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
    if (video.seekable.length === 0 && attempts < 20) {
      attempts += 1;
      setTimeout(apply, 100);
      return;
    }
    if (state.replaySessions.get(cameraID)?.shouldPlay === false) {
      video.pause();
    } else {
      video.play().catch(() => {
        if (!isCurrentReplay(generation, selectedDate)) return;
        setStreamLoading(state.replayLoaders.get(cameraID), false);
        showReplayMessage(cameraID, "这段录像暂时无法播放");
      });
    }
  };
  if (video.readyState >= HTMLMediaElement.HAVE_METADATA) {
    apply();
  } else {
    video.addEventListener("loadedmetadata", apply, {once: true});
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

function cancelReplayWork(stopPlayers = true, abortRecordings = true) {
  state.replayGeneration += 1;
  if (abortRecordings) {
    state.recordingsAbortController?.abort();
    state.recordingsAbortController = null;
    timelineViewport.classList.remove("is-disabled", "is-scrubbing");
    timelineViewport.setAttribute("aria-disabled", "false");
  }
  cancelTimelineMotion();
  state.timelineGesture = null;
  if (stopPlayers) {
    state.timelineFollowing = true;
    state.timelineSnapDirection = 0;
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
