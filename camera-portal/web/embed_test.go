package webassets

import (
	"strings"
	"testing"
)

func TestClientPageUsesVersionedFirstPartyAssets(t *testing.T) {
	data, err := FS.ReadFile("client.html")
	if err != nil {
		t.Fatal(err)
	}
	html := string(data)
	for _, asset := range []string{
		"/assets/common.css?v=20260803-1",
		"/assets/client.js?v=20260810-1",
	} {
		if !strings.Contains(html, asset) {
			t.Fatalf("client.html missing versioned asset %q", asset)
		}
	}
}

func TestReplayTimelineUsesFixedCenterScrubber(t *testing.T) {
	htmlData, err := FS.ReadFile("client.html")
	if err != nil {
		t.Fatal(err)
	}
	html := string(htmlData)
	for _, content := range []string{
		`id="timelineViewport"`,
		`class="timeline-cursor"`,
		`id="timelineRuler"`,
		`id="availability"`,
		`aria-valuetext="00:00:00"`,
	} {
		if !strings.Contains(html, content) {
			t.Fatalf("client.html missing fixed-center timeline structure %q", content)
		}
	}

	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`TIMELINE_VISIBLE_SECONDS = 2 * 60 * 60`,
		`TIMELINE_HISTORY_SECONDS = 14 * DAY_SECONDS`,
		`splitTimeRange(`,
		`state.timelineSelectedTimestamp`,
		`preparePlaybackRecordings(`,
		`bindTimelinePointerControls()`,
		`startTimelineInertia(`,
		`settleTimelineSelection(`,
		`state.hlsByCamera.get(cameraID)?.stopLoad?.()`,
		`cancelReplayWork(false, false)`,
		`state.recordingsDate !== selectedDate`,
		`video.addEventListener("timeupdate"`,
		`timelineViewport.addEventListener("lostpointercapture"`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("client.js missing fixed-center timeline behavior %q", content)
		}
	}

	styleData, err := FS.ReadFile("assets/common.css")
	if err != nil {
		t.Fatal(err)
	}
	style := string(styleData)
	for _, content := range []string{
		`.timeline-viewport`,
		`.timeline-track`,
		`.timeline-cursor`,
		`.availability-lane`,
		`touch-action: none`,
	} {
		if !strings.Contains(style, content) {
			t.Fatalf("common.css missing fixed-center timeline style %q", content)
		}
	}
}

func TestUnboundPageRetriesExistingSameSiteSession(t *testing.T) {
	htmlData, err := FS.ReadFile("unbound.html")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(htmlData), "/assets/unbound.js?v=20260730-1") {
		t.Fatal("unbound.html missing the existing-session recovery script")
	}

	scriptData, err := FS.ReadFile("assets/unbound.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`fetch("/api/config"`,
		`credentials: "same-origin"`,
		`location.reload()`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("unbound.js missing session recovery behavior %q", content)
		}
	}
}

func TestClientPageOffersExactReplayTime(t *testing.T) {
	data, err := FS.ReadFile("client.html")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `id="replayTime" type="time" step="1"`) {
		t.Fatal("client.html missing exact replay time input")
	}

	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`/live/jsmpeg/`,
		`/assets/jsmpeg-player.min.js`,
		`new window.JSMpeg.VideoElement`,
		`pauseWhenHidden: false`,
		`document.addEventListener("visibilitychange"`,
		`15 * 60`,
		`calculatePlayableRanges()`,
		`nearestPlayableTime(original, state.playableRanges)`,
		`player.ondisconnect()`,
		`new AbortController()`,
		`recordingDayStartEpoch(`,
		`import("/vendor/video-rtc.js")`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("client.js missing replay/live behavior %q", content)
		}
	}
	if strings.HasPrefix(strings.TrimSpace(script), `import {VideoRTC}`) {
		t.Fatal("iOS must not eagerly import the unused video-rtc module")
	}
}

func TestReplayTimelineSnapDoesNotShowToast(t *testing.T) {
	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, message := range []string{"已跳到最近录像", "这个时间之后没有录像"} {
		if strings.Contains(script, message) {
			t.Fatalf("timeline interaction must not show obstructive toast %q", message)
		}
	}
}

func TestReplayUsesFrigateStyleHourlyPlaylistAndSmallBuffer(t *testing.T) {
	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`const REPLAY_WINDOW_SECONDS = 60 * 60`,
		`calculateInpointOffset(`,
		`calculateSeekPosition(`,
		`startPosition`,
		`maxBufferLength: 10`,
		`maxBufferSize: 20 * 1000 * 1000`,
		`advanceReplayAfterEnd(`,
		`"loadeddata"`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("client.js missing Frigate-style replay behavior %q", content)
		}
	}
}

func TestReplayFullscreenStopsOtherCameraAndRestoresGrid(t *testing.T) {
	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`focusReplayCamera(`,
		`stopReplay(id)`,
		`restoreReplayGrid(`,
		`document.addEventListener("fullscreenchange"`,
		`video.addEventListener("webkitendfullscreen"`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("client.js missing fullscreen single-replay behavior %q", content)
		}
	}
}

func TestEachCameraRecoversIndependentlyFromAStalledFirstFrame(t *testing.T) {
	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`restartLiveCamera(`,
		`armLiveWatchdog(`,
		`state.livePlayers.get(cameraID) !== entry`,
		`armReplayWatchdog(`,
		`state.replayWatchdogs.get(cameraID) !== watchdog`,
		`playHLS(cameraID, replay, generation, selectedDate)`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("client.js missing independent stream recovery behavior %q", content)
		}
	}
}

func TestViewerShowsNonBlockingVideoLoadingIndicator(t *testing.T) {
	scriptData, err := FS.ReadFile("assets/client.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`className = "stream-loader"`,
		`setStreamLoading(`,
		`"playing"`,
		`"waiting"`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("client.js missing loading behavior %q", content)
		}
	}

	styleData, err := FS.ReadFile("assets/common.css")
	if err != nil {
		t.Fatal(err)
	}
	style := string(styleData)
	for _, content := range []string{
		`.stream-loader`,
		`pointer-events: none`,
		`animation: stream-spin`,
	} {
		if !strings.Contains(style, content) {
			t.Fatalf("common.css missing loading style %q", content)
		}
	}
}

func TestAdminPageSeparatesActiveDevicesAndCollapsedHistory(t *testing.T) {
	htmlData, err := FS.ReadFile("admin.html")
	if err != nil {
		t.Fatal(err)
	}
	html := string(htmlData)
	for _, content := range []string{
		`id="deviceList"`,
		`<details id="historyPanel" class="history-panel">`,
		`id="historyList"`,
		`id="historyCount"`,
		`/assets/admin.css?v=20260730-2`,
		`/assets/admin.js?v=20260730-3`,
	} {
		if !strings.Contains(html, content) {
			t.Fatalf("admin.html missing %q", content)
		}
	}

	scriptData, err := FS.ReadFile("assets/admin.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`devices.filter((item) => !item.revoked_at)`,
		`.filter((item) => item.revoked_at)`,
		`historyList.append(renderDevice(device, true))`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("admin.js missing device separation %q", content)
		}
	}
}
