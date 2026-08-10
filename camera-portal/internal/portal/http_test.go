package portal

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
	"time"
)

const testLocalAccessKey = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func newTestApp(t *testing.T, upstreamURL string) (*App, *Store) {
	t.Helper()
	store, err := OpenStore(filepath.Join(t.TempDir(), "state.json"), time.Now)
	if err != nil {
		t.Fatal(err)
	}
	static := fstest.MapFS{
		"client.html":       &fstest.MapFile{Data: []byte("client")},
		"unbound.html":      &fstest.MapFile{Data: []byte("unbound")},
		"pair.html":         &fstest.MapFile{Data: []byte("pair")},
		"pair-invalid.html": &fstest.MapFile{Data: []byte("invalid")},
		"admin.html":        &fstest.MapFile{Data: []byte("admin")},
		"assets/client.js":  &fstest.MapFile{Data: []byte("export {};")},
		"assets/timeline-core.mjs": &fstest.MapFile{Data: []byte("export {};")},
		"assets/unbound.js": &fstest.MapFile{Data: []byte("fetch('/api/config');")},
		"assets/hls.min.js": &fstest.MapFile{Data: []byte("window.Hls = {};")},
		"assets/jsmpeg-player.min.js": &fstest.MapFile{
			Data: []byte("window.JSMpeg = {};"),
		},
	}
	app, err := NewApp(Config{
		PublicBaseURL:  "https://example.test:8443",
		LocalBaseURL:   "http://192.168.1.250:8088",
		LocalAccessKey: testLocalAccessKey,
		AdminOrigin:    "https://camera-admin.test:9443",
		AdminLogin:     "owner@example.com",
		FrigateURL:     upstreamURL,
		Go2RTCURL:      upstreamURL,
		Cameras:        []Camera{{ID: "ptz_view", Name: "云台"}, {ID: "fixed_view", Name: "枪机"}},
		MaxLiveStreams: 2,
	}, store, static, nil)
	if err != nil {
		t.Fatal(err)
	}
	return app, store
}

func TestLocalAccessIsAuthFreeButPublicStillRequiresBinding(t *testing.T) {
	app, store := newTestApp(t, "http://frigate.test")

	localHome := httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/", nil)
	localHome.Header.Set(localAccessHeader, testLocalAccessKey)
	localHomeResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(localHomeResponse, localHome)
	if localHomeResponse.Code != http.StatusOK || localHomeResponse.Body.String() != "client" {
		t.Fatalf("LAN home = %d / %q", localHomeResponse.Code, localHomeResponse.Body.String())
	}
	if len(localHomeResponse.Result().Cookies()) != 0 {
		t.Fatal("auth-free LAN home unexpectedly issued a cookie")
	}

	localConfig := httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/api/config", nil)
	localConfig.Header.Set(localAccessHeader, testLocalAccessKey)
	localConfig.Header.Set("X-Forwarded-For", "192.168.1.30")
	localConfigResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(localConfigResponse, localConfig)
	if localConfigResponse.Code != http.StatusOK {
		t.Fatalf("auth-free LAN API status = %d", localConfigResponse.Code)
	}
	if len(store.ListDevices()) != 0 {
		t.Fatal("auth-free LAN access created a managed device")
	}

	publicHome := httptest.NewRequest(http.MethodGet, "https://example.test:8443/", nil)
	publicHomeResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(publicHomeResponse, publicHome)
	if publicHomeResponse.Code != http.StatusOK || publicHomeResponse.Body.String() != "unbound" {
		t.Fatalf("unbound public home = %d / %q", publicHomeResponse.Code, publicHomeResponse.Body.String())
	}
	publicConfig := httptest.NewRequest(http.MethodGet, "https://example.test:8443/api/config", nil)
	publicConfigResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(publicConfigResponse, publicConfig)
	if publicConfigResponse.Code != http.StatusUnauthorized {
		t.Fatalf("unbound public API status = %d", publicConfigResponse.Code)
	}

	publicCookie := pairSession(t, app, store)
	boundPublicHome := httptest.NewRequest(http.MethodGet, "https://example.test:8443/", nil)
	boundPublicHome.AddCookie(publicCookie)
	boundPublicHomeResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(boundPublicHomeResponse, boundPublicHome)
	if boundPublicHomeResponse.Code != http.StatusOK ||
		boundPublicHomeResponse.Body.String() != "client" {
		t.Fatalf("bound public home = %d / %q",
			boundPublicHomeResponse.Code, boundPublicHomeResponse.Body.String())
	}
}

func TestLocalAccessRequiresTrustedProxySecretAndHost(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")

	noSecret := httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/api/config", nil)
	noSecretResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(noSecretResponse, noSecret)
	if noSecretResponse.Code != http.StatusUnauthorized {
		t.Fatalf("missing LAN proxy secret status = %d", noSecretResponse.Code)
	}

	wrongHost := httptest.NewRequest(http.MethodGet, "http://192.168.1.251:8088/api/config", nil)
	wrongHost.Header.Set(localAccessHeader, testLocalAccessKey)
	wrongHostResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(wrongHostResponse, wrongHost)
	if wrongHostResponse.Code != http.StatusUnauthorized {
		t.Fatalf("wrong LAN host status = %d", wrongHostResponse.Code)
	}

	valid := httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/api/config", nil)
	valid.Header.Set(localAccessHeader, testLocalAccessKey)
	validResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(validResponse, valid)
	if validResponse.Code != http.StatusOK {
		t.Fatalf("trusted LAN proxy status = %d", validResponse.Code)
	}
}

func TestLocalAccessUsesStablePerIPQuotaIdentity(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	var ids []string
	handler := app.requireDevice(false, http.HandlerFunc(func(
		_ http.ResponseWriter,
		r *http.Request,
	) {
		device, ok := deviceFromContext(r.Context())
		if !ok {
			t.Fatal("LAN request is missing its quota identity")
		}
		ids = append(ids, device.ID)
	}))

	for _, ip := range []string{"192.168.1.30", "192.168.1.30", "192.168.1.31"} {
		request := httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/api/config", nil)
		request.Header.Set(localAccessHeader, testLocalAccessKey)
		request.Header.Set("X-Forwarded-For", ip)
		handler.ServeHTTP(httptest.NewRecorder(), request)
	}

	if len(ids) != 3 || ids[0] != ids[1] || ids[0] == ids[2] {
		t.Fatalf("unexpected LAN quota identities: %#v", ids)
	}
}

func TestRemovedLocalHandoffRoutesReturnNotFound(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	tests := []struct {
		method string
		target string
		local  bool
	}{
		{http.MethodGet, "https://example.test:8443/local/authorize", false},
		{http.MethodGet, "http://192.168.1.250:8088/local/bridge", true},
		{http.MethodPost, "http://192.168.1.250:8088/api/local/redeem", true},
	}
	for _, test := range tests {
		request := httptest.NewRequest(test.method, test.target, nil)
		if test.local {
			request.Header.Set(localAccessHeader, testLocalAccessKey)
		}
		response := httptest.NewRecorder()
		app.PublicHandler().ServeHTTP(response, request)
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s %s status = %d", test.method, test.target, response.Code)
		}
	}
}

func pairSession(t *testing.T, app *App, store *Store) *http.Cookie {
	t.Helper()
	_, token, err := store.CreatePairing("", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	body := `{"token":"` + token + `","metadata":{"platform":"iPhone","browser":"Safari"}}`
	request := httptest.NewRequest(http.MethodPost, "/api/pair/redeem", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Sec-Fetch-Site", "same-origin")
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusCreated {
		t.Fatalf("redeem status = %d, body = %s", response.Code, response.Body.String())
	}
	result := response.Result()
	for _, cookie := range result.Cookies() {
		if cookie.Name == sessionCookieName {
			return cookie
		}
	}
	t.Fatal("session cookie missing")
	return nil
}

func TestPublicPairingRequiresSameOriginAndSetsSecureCookie(t *testing.T) {
	app, store := newTestApp(t, "http://frigate.test")
	_, token, err := store.CreatePairing("", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	body := `{"token":"` + token + `","metadata":{}}`
	request := httptest.NewRequest(http.MethodPost, "/api/pair/redeem", strings.NewReader(body))
	request.Header.Set("Origin", "https://evil.test")
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("cross-origin status = %d", response.Code)
	}

	cookie := pairSession(t, app, store)
	if !cookie.Secure || !cookie.HttpOnly || cookie.SameSite != http.SameSiteLaxMode {
		t.Fatalf("insecure session cookie: %#v", cookie)
	}
	if cookie.MaxAge != 365*24*60*60 || cookie.Expires.Before(time.Now().Add(364*24*time.Hour)) {
		t.Fatalf("session cookie is not persistently stored for one year: %#v", cookie)
	}
}

func TestPublicAndAdminSurfacesAreSeparated(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	publicRequest := httptest.NewRequest(http.MethodGet, "/api/devices", nil)
	publicResponse := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(publicResponse, publicRequest)
	if publicResponse.Code != http.StatusNotFound {
		t.Fatalf("public admin endpoint status = %d, want 404", publicResponse.Code)
	}

	adminRequest := httptest.NewRequest(http.MethodGet, "/api/devices", nil)
	adminResponse := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(adminResponse, adminRequest)
	if adminResponse.Code != http.StatusForbidden {
		t.Fatalf("missing identity status = %d", adminResponse.Code)
	}
	adminRequest = httptest.NewRequest(http.MethodGet, "/api/devices", nil)
	adminRequest.Header.Set("Tailscale-User-Login", "other@example.com")
	adminResponse = httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(adminResponse, adminRequest)
	if adminResponse.Code != http.StatusForbidden {
		t.Fatalf("wrong identity status = %d", adminResponse.Code)
	}
	adminRequest = httptest.NewRequest(http.MethodGet, "/api/devices", nil)
	adminRequest.Header.Set("Tailscale-User-Login", "OWNER@example.com")
	adminResponse = httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(adminResponse, adminRequest)
	if adminResponse.Code != http.StatusOK {
		t.Fatalf("allowed identity status = %d, body = %s", adminResponse.Code, adminResponse.Body.String())
	}
}

func TestAdminMutationsRequireOriginAndCustomHeader(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	newRequest := func() *http.Request {
		request := httptest.NewRequest(http.MethodPost, "/api/pairings", strings.NewReader(`{"label":"测试"}`))
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Tailscale-User-Login", "owner@example.com")
		return request
	}
	request := newRequest()
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("missing CSRF checks status = %d", response.Code)
	}

	request = newRequest()
	request.Header.Set("Origin", "https://camera-admin.test:9443")
	request.Header.Set("X-Camera-Admin", "1")
	request.Header.Set("Sec-Fetch-Site", "same-origin")
	response = httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)
	if response.Code != http.StatusCreated {
		t.Fatalf("valid admin mutation status = %d, body = %s", response.Code, response.Body.String())
	}
}

func TestMediaProxyAllowlistAndTimeValidation(t *testing.T) {
	var gotPath, gotQuery, gotCookie, gotLocalKey string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotCookie = r.Header.Get("Cookie")
		gotLocalKey = r.Header.Get(localAccessHeader)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "public, max-age=7200")
		_, _ = io.WriteString(w, `[]`)
	}))
	defer upstream.Close()
	app, store := newTestApp(t, upstream.URL)
	cookie := pairSession(t, app, store)

	request := httptest.NewRequest(http.MethodGet, "/api/recordings?camera=ptz_view&after=1785306261&before=1785306329", nil)
	request.Header.Set(localAccessHeader, "attacker-controlled-value")
	request.AddCookie(cookie)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("recordings status = %d, body = %s", response.Code, response.Body.String())
	}
	assertPrivateNoStore(t, response)
	if gotPath != "/api/ptz_view/recordings" || gotCookie != "" {
		t.Fatalf("upstream path/cookie = %q / %q", gotPath, gotCookie)
	}
	if gotLocalKey != "" {
		t.Fatalf("upstream received LAN proxy secret header %q", gotLocalKey)
	}
	values, _ := url.ParseQuery(gotQuery)
	if values.Get("after") != "1785306261" || values.Get("before") != "1785306329" {
		t.Fatalf("unexpected query: %q", gotQuery)
	}

	request = httptest.NewRequest(http.MethodGet, "/api/recordings?camera=unknown&after=1785306261&before=1785306329", nil)
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("unknown camera status = %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/media/vod/ptz_view/start/1785306261/end/1785306329/master.m3u8", nil)
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK || gotPath != "/vod/ptz_view/start/1785306261/end/1785306329/master.m3u8" {
		t.Fatalf("VOD status/path = %d / %q", response.Code, gotPath)
	}
	assertPrivateNoStore(t, response)

	request = httptest.NewRequest(http.MethodGet, "/media/vod/ptz_view/start/1785302400/end/1785306000/master.m3u8", nil)
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("one-hour VOD status = %d, body = %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodGet, "/media/vod/ptz_view/start/1785302400/end/1785306001/master.m3u8", nil)
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("over-one-hour VOD status = %d", response.Code)
	}
}

func TestVODChildRequestsShareOneReplayQuotaSlot(t *testing.T) {
	started := make(chan string, 8)
	releaseUpstream := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(releaseUpstream) })

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- r.URL.Path
		<-releaseUpstream
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	app, store := newTestApp(t, upstream.URL)
	cookie := pairSession(t, app, store)
	handler := app.PublicHandler()
	files := []string{
		"master.m3u8",
		"init.mp4",
		"segment-001.m4s",
		"segment-002.m4s",
		"segment-003.m4s",
	}
	responses := make(chan int, len(files))
	for _, filename := range files {
		filename := filename
		go func() {
			request := httptest.NewRequest(
				http.MethodGet,
				"/media/vod/ptz_view/start/1785302400/end/1785303000/"+filename,
				nil,
			)
			request.AddCookie(cookie)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			responses <- response.Code
		}()
	}

	for range files {
		select {
		case <-started:
		case status := <-responses:
			t.Fatalf("VOD child request completed with status %d before the shared session reached upstream", status)
		case <-time.After(time.Second):
			t.Fatal("timed out waiting for VOD child requests to reach upstream")
		}
	}

	releaseOnce.Do(func() { close(releaseUpstream) })
	for range files {
		if status := <-responses; status != http.StatusOK {
			t.Fatalf("VOD child request status = %d, want 200", status)
		}
	}
}

func TestVODDistinctReplaySessionsStillEnforceDeviceQuota(t *testing.T) {
	started := make(chan string, 8)
	releaseUpstream := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(releaseUpstream) })

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- r.URL.Path
		<-releaseUpstream
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	app, store := newTestApp(t, upstream.URL)
	cookie := pairSession(t, app, store)
	handler := app.PublicHandler()
	responses := make(chan int, 4)
	for i := 0; i < 4; i++ {
		start := int64(1785302400 + i*600)
		requestPath := fmt.Sprintf(
			"/media/vod/ptz_view/start/%d/end/%d/master.m3u8",
			start,
			start+600,
		)
		go func() {
			request := httptest.NewRequest(http.MethodGet, requestPath, nil)
			request.AddCookie(cookie)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			responses <- response.Code
		}()
	}
	for i := 0; i < 4; i++ {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatal("timed out waiting for distinct replay sessions to reach upstream")
		}
	}

	fifthResponse := make(chan int, 1)
	go func() {
		request := httptest.NewRequest(
			http.MethodGet,
			"/media/vod/fixed_view/start/1785302400/end/1785303000/master.m3u8",
			nil,
		)
		request.AddCookie(cookie)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		fifthResponse <- response.Code
	}()
	select {
	case status := <-fifthResponse:
		if status != http.StatusTooManyRequests {
			t.Fatalf("fifth replay session status = %d, want 429", status)
		}
	case path := <-started:
		t.Fatalf("fifth replay session bypassed the device quota: %s", path)
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for replay quota response")
	}

	releaseOnce.Do(func() { close(releaseUpstream) })
	for i := 0; i < 4; i++ {
		if status := <-responses; status != http.StatusOK {
			t.Fatalf("allowed replay session status = %d, want 200", status)
		}
	}
}

func TestRetiredUnauditableLivePlaylistRoutesAreNotPublished(t *testing.T) {
	app, store := newTestApp(t, "http://frigate.test")
	cookie := pairSession(t, app, store)
	for _, route := range []string{
		"/live/api/stream.m3u8?src=ptz_view",
		"/live/api/hls/playlist.m3u8?id=test",
	} {
		request := httptest.NewRequest(http.MethodGet, route, nil)
		request.AddCookie(cookie)
		response := httptest.NewRecorder()
		app.PublicHandler().ServeHTTP(response, request)
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s status = %d, want 404", route, response.Code)
		}
	}
}

func TestJSMpegLiveUsesAuthenticatedCameraAllowlist(t *testing.T) {
	var gotPath, gotCookie string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotCookie = r.Header.Get("Cookie")
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	app, store := newTestApp(t, upstream.URL)
	cookie := pairSession(t, app, store)

	request := httptest.NewRequest(http.MethodGet, "/live/jsmpeg/ptz_view", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Upgrade", "websocket")
	request.AddCookie(cookie)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("jsmpeg status = %d, body = %s", response.Code, response.Body.String())
	}
	if gotPath != "/live/jsmpeg/ptz_view" || gotCookie != "" {
		t.Fatalf("jsmpeg upstream path/cookie = %q / %q", gotPath, gotCookie)
	}
	assertPrivateNoStore(t, response)

	request = httptest.NewRequest(http.MethodGet, "/live/jsmpeg/unknown", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Upgrade", "websocket")
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("unknown jsmpeg camera status = %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/live/jsmpeg/ptz_view", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Upgrade", "websocket")
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated jsmpeg status = %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/live/jsmpeg/ptz_view", nil)
	request.Header.Set("Origin", "https://other.example")
	request.Header.Set("Upgrade", "websocket")
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("cross-origin jsmpeg status = %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/live/jsmpeg/ptz_view", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("non-websocket jsmpeg status = %d", response.Code)
	}
}

func TestMSELiveRequiresSameOriginWebSocket(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	app, store := newTestApp(t, upstream.URL)
	cookie := pairSession(t, app, store)

	request := httptest.NewRequest(http.MethodGet, "/live/mse/api/ws?src=ptz_view", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Upgrade", "websocket")
	request.AddCookie(cookie)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("valid MSE status = %d, body = %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodGet, "/live/mse/api/ws?src=ptz_view", nil)
	request.Header.Set("Origin", "https://other.example")
	request.Header.Set("Upgrade", "websocket")
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("cross-origin MSE status = %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/live/mse/api/ws?src=ptz_view", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.AddCookie(cookie)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("non-websocket MSE status = %d", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/live/mse/api/ws?src=ptz_view", nil)
	request.Header.Set(localAccessHeader, testLocalAccessKey)
	request.Header.Set("X-Forwarded-For", "192.168.1.30")
	request.Header.Set("Origin", "http://192.168.1.250:8088")
	request.Header.Set("Upgrade", "websocket")
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("valid LAN MSE status = %d, body = %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodGet, "http://192.168.1.250:8088/live/mse/api/ws?src=ptz_view", nil)
	request.Header.Set(localAccessHeader, testLocalAccessKey)
	request.Header.Set("X-Forwarded-For", "192.168.1.30")
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Upgrade", "websocket")
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("public Origin on LAN MSE status = %d", response.Code)
	}
}

func TestPublicConfigUsesRecordingTimezone(t *testing.T) {
	app, store := newTestApp(t, "http://frigate.test")
	cookie := pairSession(t, app, store)
	request := httptest.NewRequest(http.MethodGet, "/api/config", nil)
	request.AddCookie(cookie)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatal(response.Code)
	}
	var payload struct {
		Timezone string `json:"recording_timezone"`
		Offset   int    `json:"recording_utc_offset_seconds"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Timezone != "Asia/Shanghai" || payload.Offset != 8*60*60 {
		t.Fatalf("recording timezone = %q / %d", payload.Timezone, payload.Offset)
	}
}

func TestRevokedDeviceCannotReauthorizeLive(t *testing.T) {
	app, store := newTestApp(t, "http://frigate.test")
	cookie := pairSession(t, app, store)
	device, ok := store.Authenticate(cookie.Value, false, "")
	if !ok {
		t.Fatal("paired device did not authenticate")
	}
	if err := store.RevokeDevice(device.ID); err != nil {
		t.Fatal(err)
	}
	app.liveRegistry.Revoke(device.ID)

	request := httptest.NewRequest(http.MethodGet, "/live/jsmpeg/ptz_view", nil)
	request.Header.Set("Origin", "https://example.test:8443")
	request.Header.Set("Upgrade", "websocket")
	request.AddCookie(cookie)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("revoked live reauthorization status = %d", response.Code)
	}
}

func TestJSMpegPlaybackLibraryIsServed(t *testing.T) {
	upstream := httptest.NewServer(http.NotFoundHandler())
	defer upstream.Close()
	app, _ := newTestApp(t, upstream.URL)

	request := httptest.NewRequest(http.MethodGet, "/assets/jsmpeg-player.min.js", nil)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("jsmpeg library status = %d", response.Code)
	}
	if !strings.Contains(response.Header().Get("Content-Type"), "javascript") {
		t.Fatalf("jsmpeg library content type = %q", response.Header().Get("Content-Type"))
	}
	if response.Body.String() != "window.JSMpeg = {};" {
		t.Fatalf("unexpected jsmpeg library body = %q", response.Body.String())
	}
}

func TestJSMpegLiveProxiesWebSocketAndReleasesSlot(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/live/jsmpeg/ptz_view" {
			t.Errorf("upstream path = %q", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		if r.Header.Get("Cookie") != "" {
			t.Errorf("upstream received device cookie")
		}
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Error("upstream does not support hijacking")
			return
		}
		connection, buffer, err := hijacker.Hijack()
		if err != nil {
			t.Errorf("hijack upstream: %v", err)
			return
		}
		defer connection.Close()
		acceptHash := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
		accept := base64.StdEncoding.EncodeToString(acceptHash[:])
		_, _ = buffer.WriteString(
			"HTTP/1.1 101 Switching Protocols\r\n" +
				"Connection: Upgrade\r\n" +
				"Upgrade: websocket\r\n" +
				"Sec-WebSocket-Accept: " + accept + "\r\n\r\n",
		)
		_ = buffer.Flush()
		_, _ = connection.Write([]byte{0x82, 0x02, 'o', 'k'})

		frame := make([]byte, 7)
		if _, err := io.ReadFull(connection, frame); err != nil {
			return
		}
		if frame[0] != 0x82 || frame[1] != 0x81 {
			t.Errorf("unexpected client websocket frame: %v", frame)
			return
		}
		payload := frame[6] ^ frame[2]
		_, _ = connection.Write([]byte{0x82, 0x01, payload})
		_ = connection.SetReadDeadline(time.Now().Add(3 * time.Second))
		var untilProxyCloses [1]byte
		_, _ = connection.Read(untilProxyCloses[:])
	}))
	defer upstream.Close()
	app, store := newTestApp(t, upstream.URL)
	app.liveRegistry.lease = 100 * time.Millisecond
	cookie := pairSession(t, app, store)
	public := httptest.NewServer(app.PublicHandler())
	defer public.Close()

	address := strings.TrimPrefix(public.URL, "http://")
	connection, err := net.DialTimeout("tcp", address, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	key := "dGhlIHNhbXBsZSBub25jZQ=="
	_, _ = io.WriteString(connection,
		"GET /live/jsmpeg/ptz_view HTTP/1.1\r\n"+
			"Host: "+address+"\r\n"+
			"Origin: https://example.test:8443\r\n"+
			"Cookie: "+cookie.Name+"="+cookie.Value+"\r\n"+
			"Connection: Upgrade\r\n"+
			"Upgrade: websocket\r\n"+
			"Sec-WebSocket-Version: 13\r\n"+
			"Sec-WebSocket-Key: "+key+"\r\n\r\n",
	)
	reader := bufio.NewReader(connection)
	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(status, " 101 ") {
		t.Fatalf("websocket status = %q", strings.TrimSpace(status))
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if line == "\r\n" {
			break
		}
	}
	firstFrame := make([]byte, 4)
	if _, err := io.ReadFull(reader, firstFrame); err != nil {
		t.Fatal(err)
	}
	if string(firstFrame[2:]) != "ok" {
		t.Fatalf("unexpected upstream frame: %v", firstFrame)
	}

	mask := []byte{1, 2, 3, 4}
	_, _ = connection.Write([]byte{0x82, 0x81, mask[0], mask[1], mask[2], mask[3], 'x' ^ mask[0]})
	echo := make([]byte, 3)
	if _, err := io.ReadFull(reader, echo); err != nil {
		t.Fatal(err)
	}
	if echo[0] != 0x82 || echo[1] != 0x01 || echo[2] != 'x' {
		t.Fatalf("unexpected echoed frame: %v", echo)
	}
	if err := connection.SetReadDeadline(time.Now().Add(2 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := reader.ReadByte(); err == nil {
		t.Fatal("websocket remained open after live lease expired")
	}
	_ = connection.Close()

	deadline := time.Now().Add(time.Second)
	for app.liveRegistry.activeCount() != 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := app.liveRegistry.activeCount(); got != 0 {
		t.Fatalf("live slot was not released: %d", got)
	}

	configRequest, err := http.NewRequest(http.MethodGet, public.URL+"/api/config", nil)
	if err != nil {
		t.Fatal(err)
	}
	configRequest.AddCookie(cookie)
	configResponse, err := public.Client().Do(configRequest)
	if err != nil {
		t.Fatal(err)
	}
	defer configResponse.Body.Close()
	if configResponse.StatusCode != http.StatusOK {
		t.Fatalf("config status after live lease = %d, want 200", configResponse.StatusCode)
	}

	reconnected, err := net.DialTimeout("tcp", address, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer reconnected.Close()
	_, _ = io.WriteString(reconnected,
		"GET /live/jsmpeg/ptz_view HTTP/1.1\r\n"+
			"Host: "+address+"\r\n"+
			"Origin: https://example.test:8443\r\n"+
			"Cookie: "+cookie.Name+"="+cookie.Value+"\r\n"+
			"Connection: Upgrade\r\n"+
			"Upgrade: websocket\r\n"+
			"Sec-WebSocket-Version: 13\r\n"+
			"Sec-WebSocket-Key: "+key+"\r\n\r\n",
	)
	reconnectedReader := bufio.NewReader(reconnected)
	reconnectedStatus, err := reconnectedReader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reconnectedStatus, " 101 ") {
		t.Fatalf("websocket reconnect status = %q", strings.TrimSpace(reconnectedStatus))
	}
}

func assertPrivateNoStore(t *testing.T, response *httptest.ResponseRecorder) {
	t.Helper()
	if got := response.Header().Get("Cache-Control"); got != "private, no-store" {
		t.Fatalf("Cache-Control = %q, want private, no-store", got)
	}
	if got := response.Header().Get("Cloudflare-CDN-Cache-Control"); got != "no-store" {
		t.Fatalf("Cloudflare-CDN-Cache-Control = %q, want no-store", got)
	}
	if got := response.Header().Get("Vary"); got != "Cookie" {
		t.Fatalf("Vary = %q, want Cookie", got)
	}
}

func TestValidTimeRangeRejectsDurationOverflow(t *testing.T) {
	if _, _, ok := validTimeRange("1", "9223372036854775807", 15*time.Minute); ok {
		t.Fatal("overflow-sized time range was accepted")
	}
}

func TestPlaybackLibraryIsSelfHostedWithJavascriptContentType(t *testing.T) {
	app, _ := newTestApp(t, "http://frigate.test")
	request := httptest.NewRequest(http.MethodGet, "/assets/client.js?v=20260730-1", nil)
	response := httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("client.js status = %d", response.Code)
	}
	if got := response.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("client.js Cache-Control = %q, want no-store", got)
	}
	if got := response.Header().Get("Cloudflare-CDN-Cache-Control"); got != "no-store" {
		t.Fatalf("client.js Cloudflare-CDN-Cache-Control = %q, want no-store", got)
	}

	request = httptest.NewRequest(http.MethodGet, "/assets/timeline-core.mjs?v=20260810-1", nil)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("timeline-core.mjs status = %d", response.Code)
	}
	if contentType := response.Header().Get("Content-Type"); !strings.Contains(contentType, "javascript") {
		t.Fatalf("timeline-core.mjs content type = %q", contentType)
	}
	if got := response.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("timeline-core.mjs Cache-Control = %q, want no-store", got)
	}

	request = httptest.NewRequest(http.MethodGet, "/assets/hls.min.js", nil)
	response = httptest.NewRecorder()
	app.PublicHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("hls.js status = %d", response.Code)
	}
	if contentType := response.Header().Get("Content-Type"); !strings.Contains(contentType, "javascript") {
		t.Fatalf("hls.js content type = %q", contentType)
	}
	if got := response.Header().Get("Cache-Control"); got != "public, max-age=3600" {
		t.Fatalf("hls.js Cache-Control = %q, want public cache", got)
	}
}

func TestStateSchemaDoesNotExposeCredentialHashesToAdmin(t *testing.T) {
	app, store := newTestApp(t, "http://frigate.test")
	_, _, err := store.CreatePairing("测试", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/api/pairings", nil)
	request.Header.Set("Tailscale-User-Login", "owner@example.com")
	response := httptest.NewRecorder()
	app.AdminHandler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatal(response.Code)
	}
	var payload map[string][]map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	for _, pairing := range payload["pairings"] {
		if value, ok := pairing["token_hash"]; ok && value != "" {
			t.Fatal("token hash exposed to admin API")
		}
	}
}

func TestStaticFSInterfaceIsUsed(t *testing.T) {
	var _ fs.FS = fstest.MapFS{}
}
