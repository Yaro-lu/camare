package portal

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	recordPresetSilent   = "preset-record-generic"
	recordPresetAudioAAC = "preset-record-generic-audio-aac"
)

var (
	errCameraApplyInProgress = errors.New("camera audio configuration is already being applied")
	errCameraGroupInvalid    = errors.New("camera group request is invalid")
	errCameraApplyTimeout    = errors.New("camera audio configuration apply timed out")
)

// CameraGroup describes the fixed two-camera layout shown by the portal.
// Empty camera IDs intentionally preserve an unpopulated slot.
type CameraGroup struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	CameraIDs [2]string `json:"camera_ids"`
}

// ParseCameraGroups parses groupID=group name:camera1|camera2 entries separated
// by semicolons. Camera IDs are accepted only when they are present in cameras.
func ParseCameraGroups(value string, cameras []Camera) ([]CameraGroup, error) {
	allowedCameras := make(map[string]struct{}, len(cameras))
	for _, camera := range cameras {
		allowedCameras[camera.ID] = struct{}{}
	}

	var groups []CameraGroup
	for _, rawGroup := range strings.Split(value, ";") {
		rawGroup = strings.TrimSpace(rawGroup)
		if rawGroup == "" {
			continue
		}

		id, definition, ok := strings.Cut(rawGroup, "=")
		if !ok {
			return nil, fmt.Errorf("camera group %q must contain =", rawGroup)
		}
		name, rawSlots, ok := strings.Cut(definition, ":")
		if !ok {
			return nil, fmt.Errorf("camera group %q must contain :", strings.TrimSpace(id))
		}
		slots := strings.Split(rawSlots, "|")
		if len(slots) != 2 {
			return nil, fmt.Errorf("camera group %q must contain exactly two slots", strings.TrimSpace(id))
		}

		group := CameraGroup{ID: strings.TrimSpace(id), Name: strings.TrimSpace(name)}
		for index, slot := range slots {
			group.CameraIDs[index] = strings.TrimSpace(slot)
		}
		groups = append(groups, group)
	}

	if err := validateCameraGroups(groups, allowedCameras); err != nil {
		return nil, err
	}
	return groups, nil
}

func validateCameraGroups(groups []CameraGroup, allowedCameras map[string]struct{}) error {
	if len(groups) == 0 {
		return fmt.Errorf("at least one camera group is required")
	}
	seenGroups := make(map[string]struct{}, len(groups))
	seenCameras := make(map[string]struct{}, len(allowedCameras))
	for _, group := range groups {
		if !validIdentifier(group.ID) || group.Name == "" {
			return fmt.Errorf("invalid camera group %q", group.ID)
		}
		if _, exists := seenGroups[group.ID]; exists {
			return fmt.Errorf("duplicate camera group %q", group.ID)
		}
		seenGroups[group.ID] = struct{}{}
		for _, cameraID := range group.CameraIDs {
			if cameraID == "" {
				continue
			}
			if _, allowed := allowedCameras[cameraID]; !allowed {
				return fmt.Errorf("camera group %q contains unknown camera %q", group.ID, cameraID)
			}
			if _, exists := seenCameras[cameraID]; exists {
				return fmt.Errorf("camera %q appears in more than one group", cameraID)
			}
			seenCameras[cameraID] = struct{}{}
		}
	}
	return nil
}

type cameraControl struct {
	frigate  *url.URL
	client   *http.Client
	cameras  map[string]Camera
	groups   []CameraGroup
	applySem chan struct{}

	pollInterval time.Duration
	pollTimeout  time.Duration
}

type cameraGroupStatus struct {
	ID          string             `json:"id"`
	Name        string             `json:"name"`
	Active      bool               `json:"active"`
	RecordAudio *bool              `json:"record_audio"`
	Cameras     []cameraSlotStatus `json:"cameras"`
}

type cameraSlotStatus struct {
	Slot        int    `json:"slot"`
	ID          string `json:"id,omitempty"`
	Name        string `json:"name,omitempty"`
	SnapshotURL string `json:"snapshot_url,omitempty"`
}

type frigateRuntimeConfig struct {
	Cameras map[string]struct {
		FFmpeg struct {
			OutputArgs struct {
				Record json.RawMessage `json:"record"`
			} `json:"output_args"`
		} `json:"ffmpeg"`
	} `json:"cameras"`
}

type frigateStats struct {
	Cameras map[string]struct {
		PID        int     `json:"pid"`
		CapturePID int     `json:"capture_pid"`
		FFmpegPID  int     `json:"ffmpeg_pid"`
		CameraFPS  float64 `json:"camera_fps"`
		ProcessFPS float64 `json:"process_fps"`
	} `json:"cameras"`
}

func newCameraControl(frigate *url.URL, cameras map[string]Camera, groups []CameraGroup) *cameraControl {
	return &cameraControl{
		frigate:      frigate,
		client:       &http.Client{Timeout: 5 * time.Second},
		cameras:      cameras,
		groups:       groups,
		applySem:     make(chan struct{}, 1),
		pollInterval: time.Second,
		pollTimeout:  60 * time.Second,
	}
}

func (a *App) listCameraGroups(w http.ResponseWriter, r *http.Request) {
	groups, err := a.cameraControl.status(r.Context())
	if err != nil {
		a.logger.Warn("read camera group status", "error", err)
		writeError(w, http.StatusBadGateway, "摄像头配置暂时不可用")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"groups": groups})
}

func (c *cameraControl) status(ctx context.Context) ([]cameraGroupStatus, error) {
	config, err := c.getConfig(ctx)
	if err != nil {
		return nil, err
	}
	return c.statusFromConfig(config), nil
}

func (c *cameraControl) statusFromConfig(config frigateRuntimeConfig) []cameraGroupStatus {
	groups := make([]cameraGroupStatus, 0, len(c.groups))
	for _, group := range c.groups {
		status := cameraGroupStatus{
			ID:      group.ID,
			Name:    group.Name,
			Cameras: make([]cameraSlotStatus, 0, len(group.CameraIDs)),
		}
		var groupAudio *bool
		consistent := true
		for slot, cameraID := range group.CameraIDs {
			cameraStatus := cameraSlotStatus{Slot: slot}
			if cameraID != "" {
				status.Active = true
				camera := c.cameras[cameraID]
				cameraStatus.ID = camera.ID
				cameraStatus.Name = camera.Name
				cameraStatus.SnapshotURL = "/api/cameras/" + url.PathEscape(camera.ID) + "/snapshot"

				cameraConfig, exists := config.Cameras[cameraID]
				audio, known := recordAudioPreset(cameraConfig.FFmpeg.OutputArgs.Record)
				if !exists || !known || (groupAudio != nil && *groupAudio != audio) {
					consistent = false
				} else if groupAudio == nil {
					value := audio
					groupAudio = &value
				}
			}
			status.Cameras = append(status.Cameras, cameraStatus)
		}
		if status.Active && consistent {
			status.RecordAudio = groupAudio
		}
		groups = append(groups, status)
	}
	return groups
}

func recordAudioPreset(raw json.RawMessage) (bool, bool) {
	var preset string
	if len(raw) == 0 || json.Unmarshal(raw, &preset) != nil {
		return false, false
	}
	switch preset {
	case recordPresetSilent:
		return false, true
	case recordPresetAudioAAC:
		return true, true
	default:
		return false, false
	}
}

func (c *cameraControl) getConfig(ctx context.Context) (frigateRuntimeConfig, error) {
	var config frigateRuntimeConfig
	request, err := c.newRequest(ctx, http.MethodGet, "/api/config", nil)
	if err != nil {
		return config, err
	}
	response, err := c.client.Do(request)
	if err != nil {
		return config, fmt.Errorf("get Frigate config: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))
		return config, fmt.Errorf("get Frigate config: status %d", response.StatusCode)
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 4<<20))
	if err := decoder.Decode(&config); err != nil {
		return config, fmt.Errorf("decode Frigate config: %w", err)
	}
	return config, nil
}

func (c *cameraControl) newRequest(ctx context.Context, method, endpointPath string, body io.Reader) (*http.Request, error) {
	endpoint := *c.frigate
	endpoint.Path = endpointPath
	endpoint.RawPath = ""
	endpoint.RawQuery = ""
	return http.NewRequestWithContext(ctx, method, endpoint.String(), body)
}

func (a *App) setCameraGroupsRecordAudio(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Groups map[string]*bool `json:"groups"`
	}
	if err := decodeJSON(w, r, &request, 16<<10); err != nil {
		return
	}
	desiredGroups := make(map[string]bool, len(request.Groups))
	for groupID, enabled := range request.Groups {
		if enabled == nil {
			writeError(w, http.StatusBadRequest, "录音开关不能为 null")
			return
		}
		desiredGroups[groupID] = *enabled
	}
	groups, restarted, err := a.cameraControl.applyRecordAudio(r.Context(), desiredGroups)
	if err != nil {
		switch {
		case errors.Is(err, errCameraApplyInProgress):
			writeError(w, http.StatusConflict, "另一项录音配置正在应用，请稍后重试")
		case errors.Is(err, errCameraGroupInvalid):
			writeError(w, http.StatusBadRequest, err.Error())
		case errors.Is(err, errCameraApplyTimeout):
			writeError(w, http.StatusGatewayTimeout, "Frigate 重启或录像恢复超时，请检查服务状态")
		default:
			a.logger.Error("apply camera record audio", "error", err)
			writeError(w, http.StatusBadGateway, "录音配置应用失败，请检查 Frigate 状态")
		}
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"groups":    groups,
		"restarted": restarted,
	})
}

func (a *App) cameraSnapshot(w http.ResponseWriter, r *http.Request) {
	cameraID := r.PathValue("id")
	if _, ok := a.cameraByID[cameraID]; !ok {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Cache-Control", "private, no-store")
	a.proxyHTTP(a.frigate, "/api/"+cameraID+"/latest.jpg", "height=180&quality=60").ServeHTTP(w, r)
}

func (c *cameraControl) applyRecordAudio(
	ctx context.Context,
	desiredGroups map[string]bool,
) ([]cameraGroupStatus, bool, error) {
	select {
	case c.applySem <- struct{}{}:
		defer func() { <-c.applySem }()
	default:
		return nil, false, errCameraApplyInProgress
	}

	if len(desiredGroups) == 0 {
		return nil, false, fmt.Errorf("%w: 至少选择一个摄像头组", errCameraGroupInvalid)
	}
	groupByID := make(map[string]CameraGroup, len(c.groups))
	for _, group := range c.groups {
		groupByID[group.ID] = group
	}
	for groupID := range desiredGroups {
		if _, exists := groupByID[groupID]; !exists {
			return nil, false, fmt.Errorf("%w: 摄像头组 %q 不存在", errCameraGroupInvalid, groupID)
		}
	}

	current, err := c.getConfig(ctx)
	if err != nil {
		return nil, false, err
	}
	desiredCameras := make(map[string]string)
	for groupID, enabled := range desiredGroups {
		preset := recordPresetSilent
		if enabled {
			preset = recordPresetAudioAAC
		}
		for _, cameraID := range groupByID[groupID].CameraIDs {
			if cameraID != "" {
				desiredCameras[cameraID] = preset
			}
		}
	}

	updates := make(map[string]any)
	for cameraID, preset := range desiredCameras {
		cameraConfig, exists := current.Cameras[cameraID]
		if !exists {
			return nil, false, fmt.Errorf("Frigate config does not contain camera %q", cameraID)
		}
		enabled, known := recordAudioPreset(cameraConfig.FFmpeg.OutputArgs.Record)
		wantEnabled := preset == recordPresetAudioAAC
		if known && enabled == wantEnabled {
			continue
		}
		updates[cameraID] = map[string]any{
			"ffmpeg": map[string]any{
				"output_args": map[string]any{"record": preset},
			},
		}
	}
	if len(updates) == 0 {
		stats, statsErr := c.getStats(ctx)
		if statsErr != nil {
			return nil, false, statsErr
		}
		selectedCameras := make(map[string]struct{}, len(desiredCameras))
		for cameraID := range desiredCameras {
			selectedCameras[cameraID] = struct{}{}
		}
		if !statsHealthy(stats, selectedCameras) {
			return nil, false, errors.New("one or more selected cameras are unhealthy")
		}
		return c.statusFromConfig(current), false, nil
	}

	// Once the configuration write begins, finish the bounded apply operation
	// even if the browser closes the request. Releasing the semaphore early
	// would allow a second write to overlap Frigate's restart.
	applyCtx, cancel := context.WithTimeout(context.Background(), c.pollTimeout)
	defer cancel()

	payload := map[string]any{
		"requires_restart": 1,
		"config_data": map[string]any{
			"cameras": updates,
		},
	}
	if err := c.sendJSON(applyCtx, http.MethodPut, "/api/config/set", payload); err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return nil, false, fmt.Errorf("%w: %v", errCameraApplyTimeout, err)
		}
		return nil, false, err
	}
	restartErr := c.sendJSON(applyCtx, http.MethodPost, "/api/restart", nil)
	changedGroups := make(map[string]struct{})
	for groupID := range desiredGroups {
		for _, cameraID := range groupByID[groupID].CameraIDs {
			if _, changed := updates[cameraID]; changed {
				changedGroups[groupID] = struct{}{}
				break
			}
		}
	}
	affectedCameras := make(map[string]struct{})
	for groupID := range changedGroups {
		for _, cameraID := range groupByID[groupID].CameraIDs {
			if cameraID != "" {
				affectedCameras[cameraID] = struct{}{}
			}
		}
	}
	finalConfig, err := c.waitForHealthy(applyCtx, desiredCameras, affectedCameras)
	if err != nil {
		if restartErr != nil {
			return nil, true, fmt.Errorf("%w; restart request: %v", err, restartErr)
		}
		return nil, true, err
	}
	return c.statusFromConfig(finalConfig), true, nil
}

func (c *cameraControl) sendJSON(ctx context.Context, method, endpointPath string, value any) error {
	var body io.Reader
	if value != nil {
		var encoded bytes.Buffer
		if err := json.NewEncoder(&encoded).Encode(value); err != nil {
			return fmt.Errorf("encode Frigate request: %w", err)
		}
		body = &encoded
	}
	request, err := c.newRequest(ctx, method, endpointPath, body)
	if err != nil {
		return err
	}
	if value != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.client.Do(request)
	if err != nil {
		return fmt.Errorf("%s Frigate %s: %w", method, endpointPath, err)
	}
	defer response.Body.Close()
	responseBody, _ := io.ReadAll(io.LimitReader(response.Body, 4<<10))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("%s Frigate %s: status %d: %s", method, endpointPath, response.StatusCode, strings.TrimSpace(string(responseBody)))
	}
	return nil
}

func (c *cameraControl) waitForHealthy(
	ctx context.Context,
	desired map[string]string,
	affectedCameras map[string]struct{},
) (frigateRuntimeConfig, error) {
	for {
		config, err := c.getConfig(ctx)
		if err == nil && configHasPresets(config, desired) {
			stats, statsErr := c.getStats(ctx)
			if statsErr == nil && statsHealthy(stats, affectedCameras) {
				return config, nil
			}
		}
		timer := time.NewTimer(c.pollInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return frigateRuntimeConfig{}, fmt.Errorf("%w: %v", errCameraApplyTimeout, ctx.Err())
		case <-timer.C:
		}
	}
}

func (c *cameraControl) getStats(ctx context.Context) (frigateStats, error) {
	var stats frigateStats
	request, err := c.newRequest(ctx, http.MethodGet, "/api/stats", nil)
	if err != nil {
		return stats, err
	}
	response, err := c.client.Do(request)
	if err != nil {
		return stats, fmt.Errorf("get Frigate stats: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))
		return stats, fmt.Errorf("get Frigate stats: status %d", response.StatusCode)
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 4<<20))
	if err := decoder.Decode(&stats); err != nil {
		return stats, fmt.Errorf("decode Frigate stats: %w", err)
	}
	return stats, nil
}

func statsHealthy(stats frigateStats, affectedCameras map[string]struct{}) bool {
	for cameraID := range affectedCameras {
		camera, exists := stats.Cameras[cameraID]
		if !exists || camera.PID <= 0 || camera.CapturePID <= 0 || camera.FFmpegPID <= 0 ||
			camera.CameraFPS <= 0 || camera.ProcessFPS <= 0 {
			return false
		}
	}
	return true
}

func configHasPresets(config frigateRuntimeConfig, desired map[string]string) bool {
	for cameraID, preset := range desired {
		cameraConfig, exists := config.Cameras[cameraID]
		if !exists {
			return false
		}
		enabled, known := recordAudioPreset(cameraConfig.FFmpeg.OutputArgs.Record)
		if !known || enabled != (preset == recordPresetAudioAAC) {
			return false
		}
	}
	return true
}
