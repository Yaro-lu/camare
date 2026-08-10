package portal

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestParseCameraGroupsPreservesTwoSlotsIncludingEmptyPlaceholders(t *testing.T) {
	cameras := []Camera{
		{ID: "ptz_view", Name: "云台画面"},
		{ID: "fixed_view", Name: "固定画面"},
	}

	groups, err := ParseCameraGroups(
		"yard=院子:ptz_view|fixed_view;front=门前:|",
		cameras,
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(groups) != 2 {
		t.Fatalf("group count = %d, want 2", len(groups))
	}
	if groups[0].ID != "yard" || groups[0].Name != "院子" ||
		groups[0].CameraIDs != [2]string{"ptz_view", "fixed_view"} {
		t.Fatalf("yard group = %#v", groups[0])
	}
	if groups[1].ID != "front" || groups[1].Name != "门前" ||
		groups[1].CameraIDs != [2]string{"", ""} {
		t.Fatalf("front group = %#v", groups[1])
	}
}

func TestParseCameraGroupsRejectsAnythingOutsideTheCameraWhitelist(t *testing.T) {
	cameras := []Camera{{ID: "ptz_view", Name: "云台画面"}}
	tests := []string{
		"yard=院子:ptz_view|unknown",
		"yard=院子:ptz_view",
		"yard=院子:ptz_view|;front=门前:ptz_view|",
		"yard=院子:ptz_view|;yard=门前:|",
	}
	for _, value := range tests {
		if _, err := ParseCameraGroups(value, cameras); err == nil {
			t.Errorf("ParseCameraGroups(%q) unexpectedly succeeded", value)
		}
	}
}

func TestCameraGroupStatusIsAvailableOnlyOnTheAdminSurface(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/config" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"cameras": {
				"ptz_view": {"ffmpeg": {"output_args": {"record": "preset-record-generic-audio-aac"}}},
				"fixed_view": {"ffmpeg": {"output_args": {"record": "preset-record-generic-audio-aac"}}}
			}
		}`))
	}))
	defer upstream.Close()

	app, _ := newTestApp(t, upstream.URL)
	request := httptest.NewRequest(http.MethodGet, "/api/camera-groups", nil)
	request.Header.Set("Tailscale-User-Login", "owner@example.com")
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("admin camera groups status = %d, body = %s", response.Code, response.Body.String())
	}

	var payload struct {
		Groups []struct {
			ID          string `json:"id"`
			Active      bool   `json:"active"`
			RecordAudio *bool  `json:"record_audio"`
			Cameras     []struct {
				Slot        int    `json:"slot"`
				ID          string `json:"id"`
				SnapshotURL string `json:"snapshot_url"`
			} `json:"cameras"`
		} `json:"groups"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Groups) != 1 || payload.Groups[0].ID != "yard" || !payload.Groups[0].Active {
		t.Fatalf("groups = %#v", payload.Groups)
	}
	if payload.Groups[0].RecordAudio == nil || !*payload.Groups[0].RecordAudio {
		t.Fatalf("record_audio = %#v, want true", payload.Groups[0].RecordAudio)
	}
	if got := payload.Groups[0].Cameras; len(got) != 2 || got[0].Slot != 0 ||
		got[0].ID != "ptz_view" || got[0].SnapshotURL != "/api/cameras/ptz_view/snapshot" {
		t.Fatalf("cameras = %#v", got)
	}

	publicRequest := httptest.NewRequest(http.MethodGet, "/api/camera-groups", nil)
	publicResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(publicResponse, publicRequest)
	if publicResponse.Code != http.StatusNotFound {
		t.Fatalf("public camera groups status = %d, want 404", publicResponse.Code)
	}
}

func TestCameraGroupStatusUsesNullForMixedAndEmptyGroups(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"cameras":{` +
			`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
			`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetSilent + `"}}}` +
			`}}`))
	}))
	defer upstream.Close()
	groups := []CameraGroup{
		{ID: "yard", Name: "院子", CameraIDs: [2]string{"ptz_view", "fixed_view"}},
		{ID: "front", Name: "门前", CameraIDs: [2]string{"", ""}},
	}
	app, _ := newTestAppWithCameras(t, upstream.URL,
		[]Camera{{ID: "ptz_view", Name: "云台"}, {ID: "fixed_view", Name: "固定"}},
		groups,
	)
	request := httptest.NewRequest(http.MethodGet, "/api/camera-groups", nil)
	request.Header.Set("Tailscale-User-Login", "owner@example.com")
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)

	var payload struct {
		Groups []cameraGroupStatus `json:"groups"`
	}
	if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &payload) != nil {
		t.Fatalf("camera groups = %d / %s", response.Code, response.Body.String())
	}
	if payload.Groups[0].RecordAudio != nil || !payload.Groups[0].Active {
		t.Fatalf("mixed group = %#v", payload.Groups[0])
	}
	if payload.Groups[1].RecordAudio != nil || payload.Groups[1].Active || len(payload.Groups[1].Cameras) != 2 {
		t.Fatalf("empty group = %#v", payload.Groups[1])
	}
}

func TestAdminCameraSnapshotUsesFixedLowResolutionRequest(t *testing.T) {
	var gotPath, gotQuery, gotCookie string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotCookie = r.Header.Get("Cookie")
		w.Header().Set("Content-Type", "image/jpeg")
		_, _ = w.Write([]byte("jpeg"))
	}))
	defer upstream.Close()

	app, _ := newTestApp(t, upstream.URL)
	request := httptest.NewRequest(http.MethodGet, "/api/cameras/ptz_view/snapshot", nil)
	request.Header.Set("Tailscale-User-Login", "owner@example.com")
	request.Header.Set("Cookie", "secret=must-not-reach-frigate")
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)

	if response.Code != http.StatusOK || response.Body.String() != "jpeg" {
		t.Fatalf("snapshot = %d / %q", response.Code, response.Body.String())
	}
	if got := response.Header().Get("Cache-Control"); got != "private, no-store" {
		t.Fatalf("snapshot Cache-Control = %q", got)
	}
	if gotPath != "/api/ptz_view/latest.jpg" || gotQuery != "height=180&quality=60" || gotCookie != "" {
		t.Fatalf("upstream request = %q?%s, cookie = %q", gotPath, gotQuery, gotCookie)
	}

	publicRequest := httptest.NewRequest(http.MethodGet, "/api/cameras/ptz_view/snapshot", nil)
	publicResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(publicResponse, publicRequest)
	if publicResponse.Code != http.StatusNotFound {
		t.Fatalf("public camera snapshot = %d, want 404", publicResponse.Code)
	}
}

func TestRecordAudioBatchUpdatesAllChangedCamerasAndRestartsOnce(t *testing.T) {
	var mu sync.Mutex
	restarted := false
	configSetCalls := 0
	restartCalls := 0
	statsCalls := 0
	var configSetBody struct {
		RequiresRestart int `json:"requires_restart"`
		ConfigData      struct {
			Cameras map[string]struct {
				FFmpeg struct {
					OutputArgs struct {
						Record string `json:"record"`
					} `json:"output_args"`
				} `json:"ffmpeg"`
			} `json:"cameras"`
		} `json:"config_data"`
	}

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/config":
			yardPreset := recordPresetAudioAAC
			if restarted {
				yardPreset = recordPresetSilent
			}
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + yardPreset + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + yardPreset + `"}}},` +
				`"front_ptz":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
				`"front_fixed":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}}` +
				`}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/stats":
			statsCalls++
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":1,"process_fps":1},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
		case r.Method == http.MethodPut && r.URL.Path == "/api/config/set":
			configSetCalls++
			if err := json.NewDecoder(r.Body).Decode(&configSetBody); err != nil {
				t.Fatal(err)
			}
			_, _ = w.Write([]byte(`{"success":true}`))
		case r.Method == http.MethodPost && r.URL.Path == "/api/restart":
			restartCalls++
			restarted = true
			_, _ = w.Write([]byte(`{"success":true}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()

	cameras := []Camera{
		{ID: "ptz_view", Name: "院子云台"},
		{ID: "fixed_view", Name: "院子固定"},
		{ID: "front_ptz", Name: "门前云台"},
		{ID: "front_fixed", Name: "门前固定"},
	}
	groups := []CameraGroup{
		{ID: "yard", Name: "院子", CameraIDs: [2]string{"ptz_view", "fixed_view"}},
		{ID: "front", Name: "门前", CameraIDs: [2]string{"front_ptz", "front_fixed"}},
	}
	app, _ := newTestAppWithCameras(t, upstream.URL, cameras, groups)
	app.cameraControl.pollInterval = time.Millisecond
	app.cameraControl.pollTimeout = 100 * time.Millisecond

	body := bytes.NewBufferString(`{"groups":{"yard":false,"front":true}}`)
	request := httptest.NewRequest(http.MethodPut, "/api/camera-groups/record-audio", body)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Origin", "https://camera-admin.test:9443")
	request.Header.Set("Sec-Fetch-Site", "same-origin")
	request.Header.Set("X-Camera-Admin", "1")
	request.Header.Set("Tailscale-User-Login", "owner@example.com")
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("record audio batch = %d, body = %s", response.Code, response.Body.String())
	}
	if configSetCalls != 1 || restartCalls != 1 || statsCalls == 0 || configSetBody.RequiresRestart != 1 {
		t.Fatalf("config_set calls = %d, restart calls = %d, stats calls = %d, body = %#v", configSetCalls, restartCalls, statsCalls, configSetBody)
	}
	updates := configSetBody.ConfigData.Cameras
	if len(updates) != 2 ||
		updates["ptz_view"].FFmpeg.OutputArgs.Record != recordPresetSilent ||
		updates["fixed_view"].FFmpeg.OutputArgs.Record != recordPresetSilent {
		t.Fatalf("camera updates = %#v", updates)
	}
	if _, changed := updates["front_ptz"]; changed {
		t.Fatalf("unchanged camera was included: %#v", updates)
	}

	var payload struct {
		Groups []cameraGroupStatus `json:"groups"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Groups) != 2 || payload.Groups[0].RecordAudio == nil || *payload.Groups[0].RecordAudio ||
		payload.Groups[1].RecordAudio == nil || !*payload.Groups[1].RecordAudio {
		t.Fatalf("final groups = %#v", payload.Groups)
	}
}

func TestRecordAudioBatchSkipsRestartWhenStateAlreadyMatches(t *testing.T) {
	mutationCalls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			mutationCalls++
		}
		switch r.URL.Path {
		case "/api/config":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}}` +
				`}}`))
		case "/api/stats":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":1,"process_fps":1},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)

	response := applyRecordAudioRequest(app, `{"groups":{"yard":true}}`)
	if response.Code != http.StatusOK || mutationCalls != 0 {
		t.Fatalf("no-op batch = %d / %s, mutations = %d", response.Code, response.Body.String(), mutationCalls)
	}
	var payload struct {
		Restarted bool `json:"restarted"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil || payload.Restarted {
		t.Fatalf("no-op response = %s", response.Body.String())
	}
}

func TestRecordAudioBatchNoOpFailsWhenSelectedCameraIsUnhealthy(t *testing.T) {
	mutationCalls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			mutationCalls++
		}
		switch r.URL.Path {
		case "/api/config":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}}` +
				`}}`))
		case "/api/stats":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":0,"process_fps":0},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
		}
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)

	response := applyRecordAudioRequest(app, `{"groups":{"yard":true}}`)
	if response.Code != http.StatusBadGateway || mutationCalls != 0 {
		t.Fatalf("unhealthy no-op = %d / %s, mutations = %d", response.Code, response.Body.String(), mutationCalls)
	}
}

func TestRecordAudioBatchChecksEveryCameraInAChangedGroup(t *testing.T) {
	restarted := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/config":
			fixedPreset := recordPresetAudioAAC
			if restarted {
				fixedPreset = recordPresetSilent
			}
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetSilent + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + fixedPreset + `"}}}` +
				`}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/stats":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":0,"process_fps":0},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
		case r.Method == http.MethodPut:
			_, _ = w.Write([]byte(`{"success":true}`))
		case r.Method == http.MethodPost:
			restarted = true
			_, _ = w.Write([]byte(`{"success":true}`))
		}
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)
	app.cameraControl.pollInterval = time.Millisecond
	app.cameraControl.pollTimeout = 15 * time.Millisecond

	response := applyRecordAudioRequest(app, `{"groups":{"yard":false}}`)
	if response.Code != http.StatusGatewayTimeout {
		t.Fatalf("changed group unhealthy mate = %d / %s", response.Code, response.Body.String())
	}
}

func TestRecordAudioBatchRejectsConcurrentApply(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/config" {
			once.Do(func() { close(started) })
			<-release
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}}` +
				`}}`))
			return
		}
		_, _ = w.Write([]byte(`{"cameras":{` +
			`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":1,"process_fps":1},` +
			`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
			`}}`))
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)

	firstDone := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		firstDone <- applyRecordAudioRequest(app, `{"groups":{"yard":true}}`)
	}()
	<-started
	second := applyRecordAudioRequest(app, `{"groups":{"yard":false}}`)
	if second.Code != http.StatusConflict {
		t.Fatalf("concurrent apply = %d / %s", second.Code, second.Body.String())
	}
	close(release)
	if first := <-firstDone; first.Code != http.StatusOK {
		t.Fatalf("first apply = %d / %s", first.Code, first.Body.String())
	}
}

func TestRecordAudioBatchReturnsGatewayErrorWithoutRestartWhenConfigUpdateFails(t *testing.T) {
	restartCalls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet:
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}}` +
				`}}`))
		case r.Method == http.MethodPut:
			http.Error(w, "invalid config", http.StatusBadRequest)
		case r.Method == http.MethodPost:
			restartCalls++
		}
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)

	response := applyRecordAudioRequest(app, `{"groups":{"yard":false}}`)
	if response.Code != http.StatusBadGateway || restartCalls != 0 {
		t.Fatalf("failed config update = %d / %s, restarts = %d", response.Code, response.Body.String(), restartCalls)
	}
}

func TestRecordAudioBatchTimesOutUntilRestartedConfigIsVisible(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/config" {
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + recordPresetAudioAAC + `"}}}` +
				`}}`))
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/api/stats" {
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":1,"process_fps":1},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
			return
		}
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)
	app.cameraControl.pollInterval = time.Millisecond
	app.cameraControl.pollTimeout = 15 * time.Millisecond

	response := applyRecordAudioRequest(app, `{"groups":{"yard":false}}`)
	if response.Code != http.StatusGatewayTimeout {
		t.Fatalf("restart timeout = %d / %s", response.Code, response.Body.String())
	}
}

func TestRecordAudioBatchTimesOutWhileAffectedCameraProcessesRemainUnhealthy(t *testing.T) {
	restarted := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/config":
			preset := recordPresetAudioAAC
			if restarted {
				preset = recordPresetSilent
			}
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + preset + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + preset + `"}}}` +
				`}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/stats":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":0,"process_fps":0},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
		case r.Method == http.MethodPut:
			_, _ = w.Write([]byte(`{"success":true}`))
		case r.Method == http.MethodPost:
			restarted = true
			_, _ = w.Write([]byte(`{"success":true}`))
		}
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)
	app.cameraControl.pollInterval = time.Millisecond
	app.cameraControl.pollTimeout = 15 * time.Millisecond

	response := applyRecordAudioRequest(app, `{"groups":{"yard":false}}`)
	if response.Code != http.StatusGatewayTimeout {
		t.Fatalf("unhealthy camera timeout = %d / %s", response.Code, response.Body.String())
	}
}

func TestRecordAudioBatchKeepsApplyLockAfterClientRequestIsCanceled(t *testing.T) {
	configSetStarted := make(chan struct{})
	releaseConfigSet := make(chan struct{})
	restarted := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/config":
			preset := recordPresetAudioAAC
			if restarted {
				preset = recordPresetSilent
			}
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + preset + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + preset + `"}}}` +
				`}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/stats":
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":1,"process_fps":1},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
		case r.Method == http.MethodPut:
			close(configSetStarted)
			<-releaseConfigSet
			_, _ = w.Write([]byte(`{"success":true}`))
		case r.Method == http.MethodPost:
			restarted = true
			_, _ = w.Write([]byte(`{"success":true}`))
		}
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)
	app.cameraControl.pollInterval = time.Millisecond
	app.cameraControl.pollTimeout = 200 * time.Millisecond

	requestContext, cancel := context.WithCancel(context.Background())
	request := newRecordAudioRequest(`{"groups":{"yard":false}}`).WithContext(requestContext)
	firstResponse := httptest.NewRecorder()
	firstDone := make(chan struct{})
	go func() {
		app.AdminHandler().ServeHTTP(firstResponse, request)
		close(firstDone)
	}()
	<-configSetStarted
	cancel()

	second := applyRecordAudioRequest(app, `{"groups":{"yard":true}}`)
	if second.Code != http.StatusConflict {
		t.Fatalf("apply after client cancellation = %d / %s", second.Code, second.Body.String())
	}
	close(releaseConfigSet)
	<-firstDone
	if firstResponse.Code != http.StatusOK {
		t.Fatalf("canceled request apply = %d / %s", firstResponse.Code, firstResponse.Body.String())
	}
}

func TestRecordAudioBatchAcceptsRestartConnectionResetWhenNewConfigBecomesVisible(t *testing.T) {
	var mu sync.Mutex
	restarted := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Method == http.MethodGet && r.URL.Path == "/api/config" {
			preset := recordPresetAudioAAC
			if restarted {
				preset = recordPresetSilent
			}
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"ffmpeg":{"output_args":{"record":"` + preset + `"}}},` +
				`"fixed_view":{"ffmpeg":{"output_args":{"record":"` + preset + `"}}}` +
				`}}`))
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/api/stats" {
			_, _ = w.Write([]byte(`{"cameras":{` +
				`"ptz_view":{"pid":101,"capture_pid":102,"ffmpeg_pid":103,"camera_fps":1,"process_fps":1},` +
				`"fixed_view":{"pid":201,"capture_pid":202,"ffmpeg_pid":203,"camera_fps":1,"process_fps":1}` +
				`}}`))
			return
		}
		if r.Method == http.MethodPut {
			_, _ = w.Write([]byte(`{"success":true}`))
			return
		}
		restarted = true
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Fatal("response writer does not support hijacking")
		}
		connection, _, err := hijacker.Hijack()
		if err != nil {
			t.Fatal(err)
		}
		_ = connection.Close()
	}))
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)
	app.cameraControl.pollInterval = time.Millisecond
	app.cameraControl.pollTimeout = 100 * time.Millisecond

	response := applyRecordAudioRequest(app, `{"groups":{"yard":false}}`)
	if response.Code != http.StatusOK {
		t.Fatalf("restart reset = %d / %s", response.Code, response.Body.String())
	}
}

func TestRecordAudioBatchRejectsNullInsteadOfTreatingItAsDisabled(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	response := applyRecordAudioRequest(app, `{"groups":{"yard":null}}`)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("null record audio = %d / %s", response.Code, response.Body.String())
	}
}

func TestCameraControlRoutesDoNotExistOnPublicHandler(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	tests := []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/api/camera-groups"},
		{http.MethodGet, "/api/cameras/ptz_view/snapshot"},
		{http.MethodPut, "/api/camera-groups/record-audio"},
	}
	for _, test := range tests {
		request := httptest.NewRequest(test.method, test.path, bytes.NewBufferString(`{"groups":{"yard":false}}`))
		response := httptest.NewRecorder()
		app.PublicHandler().ServeHTTP(response, request)
		if response.Code != http.StatusNotFound {
			t.Errorf("public %s %s = %d, want 404", test.method, test.path, response.Code)
		}
	}
}

func applyRecordAudioRequest(app *App, body string) *httptest.ResponseRecorder {
	request := newRecordAudioRequest(body)
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)
	return response
}

func newRecordAudioRequest(body string) *http.Request {
	request := httptest.NewRequest(http.MethodPut, "/api/camera-groups/record-audio", bytes.NewBufferString(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Origin", "https://camera-admin.test:9443")
	request.Header.Set("Sec-Fetch-Site", "same-origin")
	request.Header.Set("X-Camera-Admin", "1")
	request.Header.Set("Tailscale-User-Login", "owner@example.com")
	return request
}
