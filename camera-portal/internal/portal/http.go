package portal

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path"
	"strconv"
	"strings"
	"time"
	_ "time/tzdata"

	qrcode "github.com/skip2/go-qrcode"
)

const (
	sessionCookieName   = "camera_device"
	sessionCookieMaxAge = 365 * 24 * 60 * 60
)

type Camera struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

type publicCameraGroup struct {
	ID        string   `json:"id"`
	Name      string   `json:"name"`
	CameraIDs []string `json:"camera_ids"`
}

type Config struct {
	PublicBaseURL              string
	LocalBaseURL               string
	LocalAccessKey             string
	AdminOrigin                string
	AdminLogin                 string
	FrigateURL                 string
	Go2RTCURL                  string
	RecordingTimezone          string
	Cameras                    []Camera
	CameraGroups               []CameraGroup
	MaxLiveStreams             int
	MaxLiveStreamsPerDevice    int
	LiveLease                  time.Duration
	MaxReplayRequests          int
	MaxReplayRequestsPerDevice int
}

type App struct {
	config         Config
	store          *Store
	static         fs.FS
	logger         *slog.Logger
	frigate        *url.URL
	go2rtc         *url.URL
	cameraByID     map[string]Camera
	liveRegistry   *liveRegistry
	replayLimiter  *concurrencyLimiter
	recordingZone  *time.Location
	publicBaseURL  string
	publicOrigin   string
	localOrigin    string
	localHost      string
	localAccessKey string
	adminOrigin    string
	cameraControl  *cameraControl
}

type contextKey string

const deviceContextKey contextKey = "device"

func NewApp(config Config, store *Store, static fs.FS, logger *slog.Logger) (*App, error) {
	if store == nil {
		return nil, errors.New("store is required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	publicURL, err := url.Parse(config.PublicBaseURL)
	if err != nil || publicURL.Scheme == "" || publicURL.Host == "" {
		return nil, errors.New("PUBLIC_BASE_URL must be an absolute URL")
	}
	localBaseURL := strings.TrimRight(strings.TrimSpace(config.LocalBaseURL), "/")
	localAccessKey := strings.TrimSpace(config.LocalAccessKey)
	if (localBaseURL == "") != (localAccessKey == "") {
		return nil, errors.New("LOCAL_BASE_URL and LOCAL_ACCESS_KEY must be configured together")
	}
	var localOrigin string
	if localBaseURL != "" {
		localURL, localErr := url.Parse(localBaseURL)
		if localErr != nil || localURL.Scheme != "http" || localURL.Host == "" ||
			localURL.User != nil ||
			localURL.RawQuery != "" || localURL.Fragment != "" ||
			(localURL.Path != "" && localURL.Path != "/") {
			return nil, errors.New("LOCAL_BASE_URL must be an absolute HTTP origin")
		}
		if len(localAccessKey) < 32 {
			return nil, errors.New("LOCAL_ACCESS_KEY must contain at least 32 characters")
		}
		localOrigin = localURL.Scheme + "://" + localURL.Host
	}
	frigateURL, err := url.Parse(config.FrigateURL)
	if err != nil || frigateURL.Scheme == "" || frigateURL.Host == "" {
		return nil, errors.New("FRIGATE_URL must be an absolute URL")
	}
	go2rtcURL, err := url.Parse(config.Go2RTCURL)
	if err != nil || go2rtcURL.Scheme == "" || go2rtcURL.Host == "" {
		return nil, errors.New("GO2RTC_URL must be an absolute URL")
	}
	adminURL, err := url.Parse(config.AdminOrigin)
	if err != nil || adminURL.Scheme == "" || adminURL.Host == "" {
		return nil, errors.New("ADMIN_ORIGIN must be an absolute URL")
	}
	if strings.TrimSpace(config.AdminLogin) == "" {
		return nil, errors.New("ADMIN_TAILSCALE_LOGIN is required")
	}
	if len(config.Cameras) == 0 {
		return nil, errors.New("at least one camera is required")
	}
	cameraByID := make(map[string]Camera, len(config.Cameras))
	for _, camera := range config.Cameras {
		if !validIdentifier(camera.ID) || strings.TrimSpace(camera.Name) == "" {
			return nil, fmt.Errorf("invalid camera configuration: %#v", camera)
		}
		cameraByID[camera.ID] = camera
	}
	allowedCameras := make(map[string]struct{}, len(cameraByID))
	for cameraID := range cameraByID {
		allowedCameras[cameraID] = struct{}{}
	}
	if err := validateCameraGroups(config.CameraGroups, allowedCameras); err != nil {
		return nil, fmt.Errorf("invalid CAMERA_GROUPS: %w", err)
	}
	if config.MaxLiveStreams <= 0 {
		config.MaxLiveStreams = 8
	}
	if config.MaxLiveStreamsPerDevice <= 0 {
		config.MaxLiveStreamsPerDevice = 2
	}
	if config.LiveLease <= 0 {
		config.LiveLease = 15 * time.Minute
	}
	if config.MaxReplayRequests <= 0 {
		config.MaxReplayRequests = 16
	}
	if config.MaxReplayRequestsPerDevice <= 0 {
		config.MaxReplayRequestsPerDevice = 4
	}
	if strings.TrimSpace(config.RecordingTimezone) == "" {
		config.RecordingTimezone = "Asia/Shanghai"
	}
	recordingZone, err := time.LoadLocation(config.RecordingTimezone)
	if err != nil {
		return nil, fmt.Errorf("invalid RECORDING_TIMEZONE: %w", err)
	}
	app := &App{
		config:         config,
		store:          store,
		static:         static,
		logger:         logger,
		frigate:        frigateURL,
		go2rtc:         go2rtcURL,
		cameraByID:     cameraByID,
		liveRegistry:   newLiveRegistry(config.MaxLiveStreams, config.MaxLiveStreamsPerDevice, config.LiveLease),
		replayLimiter:  newConcurrencyLimiter(config.MaxReplayRequests, config.MaxReplayRequestsPerDevice),
		recordingZone:  recordingZone,
		publicBaseURL:  strings.TrimRight(config.PublicBaseURL, "/"),
		publicOrigin:   publicURL.Scheme + "://" + publicURL.Host,
		localOrigin:    localOrigin,
		localHost:      strings.TrimPrefix(localOrigin, "http://"),
		localAccessKey: localAccessKey,
		adminOrigin:    adminURL.Scheme + "://" + adminURL.Host,
	}
	app.cameraControl = newCameraControl(frigateURL, cameraByID, config.CameraGroups)
	return app, nil
}

func (a *App) PublicHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", a.health)
	mux.HandleFunc("GET /pair/{token}", a.pairPage)
	mux.HandleFunc("POST /api/pair/redeem", a.redeemPairing)
	mux.HandleFunc("GET /assets/common.css", a.staticFile("assets/common.css"))
	mux.HandleFunc("GET /assets/client.js", a.staticFile("assets/client.js"))
	mux.HandleFunc("GET /assets/camera-groups.mjs", a.staticFile("assets/camera-groups.mjs"))
	mux.HandleFunc("GET /assets/timeline-core.mjs", a.staticFile("assets/timeline-core.mjs"))
	mux.HandleFunc("GET /assets/pair.js", a.staticFile("assets/pair.js"))
	mux.HandleFunc("GET /assets/unbound.js", a.staticFile("assets/unbound.js"))
	mux.HandleFunc("GET /assets/hls.min.js", a.staticFile("assets/hls.min.js"))
	mux.HandleFunc("GET /assets/jsmpeg-player.min.js", a.staticFile("assets/jsmpeg-player.min.js"))
	mux.HandleFunc("GET /assets/mark.svg", a.staticFile("assets/mark.svg"))
	mux.Handle("GET /vendor/video-rtc.js", a.requireDevice(false, http.HandlerFunc(a.videoRTC)))
	mux.Handle("GET /api/config", a.requireDevice(true, http.HandlerFunc(a.publicConfig)))
	mux.Handle("GET /api/recordings", a.requireDevice(false, http.HandlerFunc(a.recordings)))
	mux.Handle("GET /media/vod/", a.requireDevice(false, http.HandlerFunc(a.vod)))
	mux.Handle("GET /live/mse/api/ws", a.requireDevice(false, http.HandlerFunc(a.live)))
	mux.Handle("GET /live/jsmpeg/{camera}", a.requireDevice(false, http.HandlerFunc(a.liveJSMpeg)))
	mux.Handle("GET /live/mse/api/hls/", a.requireDevice(false, http.HandlerFunc(a.liveHLS)))
	mux.HandleFunc("GET /{$}", a.publicHome)
	return a.securityHeaders(mux)
}

func (a *App) AdminHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", a.health)
	protected := http.NewServeMux()
	protected.HandleFunc("GET /", a.adminHome)
	protected.HandleFunc("GET /assets/admin.css", a.staticFile("assets/admin.css"))
	protected.HandleFunc("GET /assets/admin.js", a.staticFile("assets/admin.js"))
	protected.HandleFunc("GET /assets/mark.svg", a.staticFile("assets/mark.svg"))
	protected.HandleFunc("GET /api/devices", a.listDevices)
	protected.HandleFunc("GET /api/pairings", a.listPairings)
	protected.HandleFunc("GET /api/camera-groups", a.listCameraGroups)
	protected.HandleFunc("GET /api/cameras/{id}/snapshot", a.cameraSnapshot)
	protected.Handle("PUT /api/camera-groups/record-audio", a.requireAdminMutation(http.HandlerFunc(a.setCameraGroupsRecordAudio)))
	protected.Handle("POST /api/pairings", a.requireAdminMutation(http.HandlerFunc(a.createPairing)))
	protected.Handle("PATCH /api/devices/{id}", a.requireAdminMutation(http.HandlerFunc(a.renameDevice)))
	protected.Handle("DELETE /api/devices/{id}", a.requireAdminMutation(http.HandlerFunc(a.revokeDevice)))
	mux.Handle("/", a.requireAdminIdentity(protected))
	return a.securityHeaders(mux)
}

func (a *App) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (a *App) publicHome(w http.ResponseWriter, r *http.Request) {
	if a.isLocalRequest(r) {
		a.serveHTML(w, r, "client.html")
		return
	}
	cookie, err := r.Cookie(sessionCookieName)
	if err != nil {
		a.serveHTML(w, r, "unbound.html")
		return
	}
	if _, ok := a.store.Authenticate(cookie.Value, true, clientIP(r)); !ok {
		a.clearSessionCookie(w)
		a.serveHTML(w, r, "unbound.html")
		return
	}
	a.setSessionCookie(w, cookie.Value)
	a.serveHTML(w, r, "client.html")
}

func (a *App) pairPage(w http.ResponseWriter, r *http.Request) {
	token := r.PathValue("token")
	if _, err := a.store.PairingStatus(token); err != nil {
		status := http.StatusGone
		if errors.Is(err, ErrPairingInvalid) {
			status = http.StatusNotFound
		}
		w.WriteHeader(status)
		a.serveHTML(w, r, "pair-invalid.html")
		return
	}
	a.serveHTML(w, r, "pair.html")
}

func (a *App) redeemPairing(w http.ResponseWriter, r *http.Request) {
	if !sameOrigin(r.Header.Get("Origin"), a.publicOrigin) ||
		!fetchSiteAllowed(r.Header.Get("Sec-Fetch-Site")) {
		writeError(w, http.StatusForbidden, "请求来源无效")
		return
	}
	var request struct {
		Token    string         `json:"token"`
		Metadata DeviceMetadata `json:"metadata"`
	}
	if err := decodeJSON(w, r, &request, 16<<10); err != nil {
		return
	}
	if len(request.Token) < 32 || len(request.Token) > 100 {
		writeError(w, http.StatusBadRequest, "二维码无效")
		return
	}
	device, session, err := a.store.Redeem(request.Token, request.Metadata, clientIP(r))
	if err != nil {
		switch {
		case errors.Is(err, ErrPairingInvalid):
			writeError(w, http.StatusNotFound, "二维码无效")
		case errors.Is(err, ErrPairingExpired), errors.Is(err, ErrPairingUsed):
			writeError(w, http.StatusGone, "二维码已过期或已使用")
		default:
			a.logger.Error("redeem pairing", "error", err)
			writeError(w, http.StatusInternalServerError, "绑定失败")
		}
		return
	}
	a.setSessionCookie(w, session)
	a.logger.Info("device paired", "device_id", device.ID, "platform", device.Platform)
	writeJSON(w, http.StatusCreated, map[string]any{"bound": true, "device": device})
}

func (a *App) publicConfig(w http.ResponseWriter, _ *http.Request) {
	now := time.Now().In(a.recordingZone)
	_, offset := now.Zone()
	writeJSON(w, http.StatusOK, map[string]any{
		"cameras":                      a.config.Cameras,
		"camera_groups":                a.publicCameraGroups(),
		"recording_timezone":           a.config.RecordingTimezone,
		"recording_utc_offset_seconds": offset,
	})
}

func (a *App) publicCameraGroups() []publicCameraGroup {
	groups := make([]publicCameraGroup, 0, len(a.config.CameraGroups))
	for _, group := range a.config.CameraGroups {
		cameraIDs := make([]string, 0, len(group.CameraIDs))
		for _, cameraID := range group.CameraIDs {
			if cameraID != "" {
				cameraIDs = append(cameraIDs, cameraID)
			}
		}
		if len(cameraIDs) == 0 {
			continue
		}
		groups = append(groups, publicCameraGroup{
			ID:        group.ID,
			Name:      group.Name,
			CameraIDs: cameraIDs,
		})
	}
	return groups
}

func (a *App) recordings(w http.ResponseWriter, r *http.Request) {
	camera := r.URL.Query().Get("camera")
	if _, ok := a.cameraByID[camera]; !ok {
		writeError(w, http.StatusBadRequest, "摄像头无效")
		return
	}
	after, before, ok := validTimeRange(
		r.URL.Query().Get("after"),
		r.URL.Query().Get("before"),
		24*time.Hour,
	)
	if !ok {
		writeError(w, http.StatusBadRequest, "时间范围无效")
		return
	}
	query := url.Values{}
	query.Set("after", strconv.FormatInt(after, 10))
	query.Set("before", strconv.FormatInt(before, 10))
	a.proxyHTTP(a.frigate, "/api/"+camera+"/recordings", query.Encode()).ServeHTTP(w, r)
}

func (a *App) vod(w http.ResponseWriter, r *http.Request) {
	device, ok := deviceFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "设备未绑定")
		return
	}
	trimmed := strings.TrimPrefix(r.URL.Path, "/media/vod/")
	parts := strings.Split(trimmed, "/")
	if len(parts) != 6 || parts[1] != "start" || parts[3] != "end" {
		http.NotFound(w, r)
		return
	}
	camera := parts[0]
	if _, ok := a.cameraByID[camera]; !ok || !validMediaFilename(parts[5]) {
		http.NotFound(w, r)
		return
	}
	start, end, ok := validTimeRange(parts[2], parts[4], time.Hour)
	if !ok {
		writeError(w, http.StatusBadRequest, "时间范围无效")
		return
	}
	sessionID := fmt.Sprintf("%s/%d/%d", camera, start, end)
	release, err := a.replayLimiter.AcquireSession(device.ID, sessionID)
	if err != nil {
		status := http.StatusTooManyRequests
		if errors.Is(err, errReplayGlobalLimit) {
			status = http.StatusServiceUnavailable
		}
		writeError(w, status, "回看请求过多，请稍后重试")
		return
	}
	defer release()

	upstreamPath := fmt.Sprintf(
		"/vod/%s/start/%d/end/%d/%s",
		camera,
		start,
		end,
		parts[5],
	)
	a.proxyHTTP(a.frigate, upstreamPath, "").ServeHTTP(w, r)
}

func (a *App) live(w http.ResponseWriter, r *http.Request) {
	source := r.URL.Query().Get("src")
	if _, ok := a.cameraByID[source]; !ok {
		writeError(w, http.StatusBadRequest, "摄像头无效")
		return
	}
	if !a.validLiveWebSocket(w, r) {
		return
	}
	query := url.Values{}
	query.Set("src", source)
	a.serveLiveWebSocket(w, r, source, "/live/mse/api/ws", query.Encode())
}

func (a *App) liveJSMpeg(w http.ResponseWriter, r *http.Request) {
	camera := r.PathValue("camera")
	if _, ok := a.cameraByID[camera]; !ok {
		writeError(w, http.StatusBadRequest, "摄像头无效")
		return
	}
	if !a.validLiveWebSocket(w, r) {
		return
	}
	a.serveLiveWebSocket(w, r, camera, "/live/jsmpeg/"+camera, "")
}

func (a *App) validLiveWebSocket(w http.ResponseWriter, r *http.Request) bool {
	expectedOrigin := a.publicOrigin
	if a.isLocalRequest(r) {
		expectedOrigin = a.localOrigin
	}
	if !sameOrigin(r.Header.Get("Origin"), expectedOrigin) {
		writeError(w, http.StatusForbidden, "请求来源无效")
		return false
	}
	if !strings.EqualFold(strings.TrimSpace(r.Header.Get("Upgrade")), "websocket") {
		writeError(w, http.StatusBadRequest, "直播连接格式无效")
		return false
	}
	return true
}

func (a *App) serveLiveWebSocket(w http.ResponseWriter, r *http.Request, camera, upstreamPath, query string) {
	device, ok := deviceFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "设备未绑定")
		return
	}
	ctx, entry, err := a.liveRegistry.Begin(r.Context(), device.ID, camera)
	if err != nil {
		switch {
		case errors.Is(err, errLiveDeviceRevoked):
			if !a.isLocalRequest(r) {
				a.clearSessionCookie(w)
			}
			writeError(w, http.StatusUnauthorized, "设备已撤销")
		case errors.Is(err, errLiveDeviceLimit):
			writeError(w, http.StatusTooManyRequests, "此设备的直播连接已满")
		default:
			writeError(w, http.StatusServiceUnavailable, "直播连接已满，请稍后重试")
		}
		return
	}
	defer a.liveRegistry.End(entry)
	a.proxyHTTP(a.frigate, upstreamPath, query).ServeHTTP(w, r.WithContext(ctx))
}

func (a *App) videoRTC(w http.ResponseWriter, r *http.Request) {
	a.proxyHTTP(a.go2rtc, "/video-rtc.js", "").ServeHTTP(w, r)
}

func (a *App) liveHLS(w http.ResponseWriter, r *http.Request) {
	a.proxyLiveHLS(w, r, "/live/mse/api/hls/")
}

func (a *App) proxyLiveHLS(w http.ResponseWriter, r *http.Request, prefix string) {
	device, ok := deviceFromContext(r.Context())
	if !ok {
		writeError(w, http.StatusUnauthorized, "设备未绑定")
		return
	}
	release, err := a.replayLimiter.Acquire(device.ID)
	if err != nil {
		status := http.StatusTooManyRequests
		if errors.Is(err, errReplayGlobalLimit) {
			status = http.StatusServiceUnavailable
		}
		writeError(w, status, "视频请求过多，请稍后重试")
		return
	}
	defer release()

	suffix := strings.TrimPrefix(r.URL.Path, prefix)
	if suffix != "" && !validMediaPath(suffix) {
		http.NotFound(w, r)
		return
	}
	query := r.URL.Query()
	if source := query.Get("src"); source != "" {
		if _, ok := a.cameraByID[source]; !ok {
			writeError(w, http.StatusBadRequest, "摄像头无效")
			return
		}
	}
	a.proxyHTTP(a.go2rtc, "/api/hls/"+suffix, query.Encode()).ServeHTTP(w, r)
}

func (a *App) adminHome(w http.ResponseWriter, r *http.Request) {
	a.serveHTML(w, r, "admin.html")
}

func (a *App) listDevices(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"devices": a.store.ListDevices()})
}

func (a *App) listPairings(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"pairings": a.store.ListPairings()})
}

func (a *App) createPairing(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Label       string `json:"label"`
		ExpiryHours int    `json:"expiry_hours"`
	}
	if err := decodeJSON(w, r, &request, 8<<10); err != nil {
		return
	}
	if request.ExpiryHours <= 0 || request.ExpiryHours > 168 {
		request.ExpiryHours = 24
	}
	pairing, token, err := a.store.CreatePairing(request.Label, time.Duration(request.ExpiryHours)*time.Hour)
	if err != nil {
		a.logger.Error("create pairing", "error", err)
		writeError(w, http.StatusInternalServerError, "生成失败")
		return
	}
	pairURL := strings.TrimRight(a.config.PublicBaseURL, "/") + "/pair/" + token
	png, err := qrcode.Encode(pairURL, qrcode.Medium, 360)
	if err != nil {
		a.logger.Error("encode pairing QR", "error", err)
		writeError(w, http.StatusInternalServerError, "二维码生成失败")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"pairing": pairing,
		"url":     pairURL,
		"qr":      "data:image/png;base64," + base64.StdEncoding.EncodeToString(png),
	})
}

func (a *App) renameDevice(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Label string `json:"label"`
	}
	if err := decodeJSON(w, r, &request, 8<<10); err != nil {
		return
	}
	device, err := a.store.RenameDevice(r.PathValue("id"), request.Label)
	if err != nil {
		if errors.Is(err, ErrDeviceNotFound) {
			writeError(w, http.StatusNotFound, "设备不存在")
		} else {
			writeError(w, http.StatusBadRequest, err.Error())
		}
		return
	}
	a.logger.Info("device renamed", "device_id", device.ID)
	writeJSON(w, http.StatusOK, map[string]any{"device": device})
}

func (a *App) revokeDevice(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := a.store.RevokeDevice(id); err != nil {
		if errors.Is(err, ErrDeviceNotFound) {
			writeError(w, http.StatusNotFound, "设备不存在")
		} else {
			writeError(w, http.StatusInternalServerError, "撤销失败")
		}
		return
	}
	a.liveRegistry.Revoke(id)
	a.logger.Info("device revoked", "device_id", id)
	w.WriteHeader(http.StatusNoContent)
}

func (a *App) requireDevice(touch bool, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a.isLocalRequest(r) {
			ip := clientIP(r)
			device := Device{
				ID:       "lan:" + ip,
				Label:    "家庭局域网",
				Platform: "LAN",
				LastIP:   ip,
			}
			ctx := context.WithValue(r.Context(), deviceContextKey, device)
			next.ServeHTTP(w, r.WithContext(ctx))
			return
		}
		cookie, err := r.Cookie(sessionCookieName)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "设备未绑定")
			return
		}
		device, ok := a.store.Authenticate(cookie.Value, touch, clientIP(r))
		if !ok {
			a.clearSessionCookie(w)
			writeError(w, http.StatusUnauthorized, "设备未绑定或已撤销")
			return
		}
		if touch {
			a.setSessionCookie(w, cookie.Value)
		}
		ctx := context.WithValue(r.Context(), deviceContextKey, device)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (a *App) requireAdminIdentity(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		login := strings.TrimSpace(r.Header.Get("Tailscale-User-Login"))
		if !strings.EqualFold(login, a.config.AdminLogin) {
			http.Error(w, "Forbidden", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (a *App) requireAdminMutation(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !sameOrigin(r.Header.Get("Origin"), a.adminOrigin) ||
			r.Header.Get("X-Camera-Admin") != "1" ||
			!fetchSiteAllowed(r.Header.Get("Sec-Fetch-Site")) {
			writeError(w, http.StatusForbidden, "请求来源无效")
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (a *App) proxyHTTP(upstream *url.URL, upstreamPath, rawQuery string) http.Handler {
	proxy := httputil.NewSingleHostReverseProxy(upstream)
	director := proxy.Director
	proxy.Director = func(request *http.Request) {
		director(request)
		request.URL.Path = upstreamPath
		request.URL.RawPath = ""
		request.URL.RawQuery = rawQuery
		request.Host = upstream.Host
		request.Header.Del("Cookie")
		request.Header.Del("Authorization")
		request.Header.Del("Tailscale-User-Login")
		request.Header.Del("Tailscale-User-Name")
		request.Header.Del(localAccessHeader)
	}
	proxy.ModifyResponse = func(response *http.Response) error {
		response.Header.Del("Set-Cookie")
		response.Header.Del("Access-Control-Allow-Origin")
		response.Header.Set("Cache-Control", "private, no-store")
		response.Header.Set("Cloudflare-CDN-Cache-Control", "no-store")
		response.Header.Set("Pragma", "no-cache")
		response.Header.Set("Vary", "Cookie")
		return nil
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		a.logger.Warn("upstream request failed", "error", err)
		writeError(w, http.StatusBadGateway, "视频服务暂时不可用")
	}
	return proxy
}

func (a *App) securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
		w.Header().Set(
			"Content-Security-Policy",
			"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "+
				"connect-src 'self' ws: wss:; media-src 'self' blob:; worker-src 'self' blob:; "+
				"frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
		)
		next.ServeHTTP(w, r)
	})
}

func (a *App) staticFile(name string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		data, err := fs.ReadFile(a.static, name)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		contentType := mime.TypeByExtension(path.Ext(name))
		if path.Ext(name) == ".mjs" {
			contentType = "text/javascript; charset=utf-8"
		}
		if contentType != "" {
			w.Header().Set("Content-Type", contentType)
		}
		if name == "assets/hls.min.js" ||
			name == "assets/jsmpeg-player.min.js" ||
			name == "assets/mark.svg" {
			w.Header().Set("Cache-Control", "public, max-age=3600")
		} else {
			// First-party UI assets change with portal deployments. Do not let
			// browsers or Cloudflare keep an older UI after the container has
			// already been upgraded.
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Cloudflare-CDN-Cache-Control", "no-store")
		}
		_, _ = w.Write(data)
	}
}

func (a *App) serveHTML(w http.ResponseWriter, r *http.Request, name string) {
	data, err := fs.ReadFile(a.static, name)
	if err != nil {
		http.Error(w, "Page unavailable", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(data)
}

func (a *App) clearSessionCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookieName,
		Value:    "",
		Path:     "/",
		Expires:  time.Unix(1, 0).UTC(),
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   true,
		SameSite: http.SameSiteLaxMode,
	})
}

func (a *App) setSessionCookie(w http.ResponseWriter, session string) {
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookieName,
		Value:    session,
		Path:     "/",
		Expires:  time.Now().UTC().Add(time.Duration(sessionCookieMaxAge) * time.Second),
		MaxAge:   sessionCookieMaxAge,
		HttpOnly: true,
		Secure:   true,
		SameSite: http.SameSiteLaxMode,
	})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, target any, limit int64) error {
	r.Body = http.MaxBytesReader(w, r.Body, limit)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		writeError(w, http.StatusBadRequest, "请求格式无效")
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "请求只能包含一个 JSON 对象")
		return errors.New("multiple JSON values")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func clientIP(r *http.Request) string {
	if forwarded := strings.TrimSpace(strings.Split(r.Header.Get("X-Forwarded-For"), ",")[0]); forwarded != "" {
		return forwarded
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err == nil {
		return host
	}
	return r.RemoteAddr
}

func sameOrigin(got, want string) bool {
	gotURL, err := url.Parse(got)
	if err != nil || gotURL.Scheme == "" || gotURL.Host == "" {
		return false
	}
	return strings.EqualFold(gotURL.Scheme+"://"+gotURL.Host, want)
}

func sameHost(got, want string) bool {
	return strings.EqualFold(strings.TrimSpace(got), strings.TrimSpace(want))
}

func fetchSiteAllowed(value string) bool {
	return value == "" || value == "same-origin"
}

func validIdentifier(value string) bool {
	if value == "" || len(value) > 80 {
		return false
	}
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || r == '_' || r == '-' {
			continue
		}
		return false
	}
	return true
}

func validMediaFilename(value string) bool {
	if value == "" || len(value) > 120 || strings.HasPrefix(value, ".") {
		return false
	}
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || r == '_' || r == '-' || r == '.' {
			continue
		}
		return false
	}
	return strings.HasSuffix(value, ".m3u8") ||
		strings.HasSuffix(value, ".mp4") ||
		strings.HasSuffix(value, ".m4s")
}

func validMediaPath(value string) bool {
	if value == "" || len(value) > 240 || strings.Contains(value, "..") {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || !validMediaFilename(part) {
			return false
		}
	}
	return true
}

func validTimeRange(startRaw, endRaw string, maximum time.Duration) (int64, int64, bool) {
	start, err := strconv.ParseInt(startRaw, 10, 64)
	if err != nil || start <= 0 {
		return 0, 0, false
	}
	end, err := strconv.ParseInt(endRaw, 10, 64)
	if err != nil || end <= start {
		return 0, 0, false
	}
	maximumSeconds := int64(maximum / time.Second)
	if maximumSeconds <= 0 || end-start > maximumSeconds {
		return 0, 0, false
	}
	return start, end, true
}

func deviceFromContext(ctx context.Context) (Device, bool) {
	device, ok := ctx.Value(deviceContextKey).(Device)
	return device, ok && device.ID != ""
}
