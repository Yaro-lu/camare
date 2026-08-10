package webassets

import (
	"strings"
	"testing"
)

func TestAdminCameraGroupsAreIndependentFromDeviceRefresh(t *testing.T) {
	htmlData, err := FS.ReadFile("admin.html")
	if err != nil {
		t.Fatal(err)
	}
	html := string(htmlData)
	for _, content := range []string{
		`id="cameraSection"`,
		`id="cameraGroups"`,
		`id="cameraApplyStatus"`,
		`id="applyCameraAudio"`,
	} {
		if !strings.Contains(html, content) {
			t.Fatalf("admin.html missing camera management structure %q", content)
		}
	}

	scriptData, err := FS.ReadFile("assets/admin.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`loadCameraGroups();`,
		`setInterval(refresh, 30000);`,
		`fetch("/api/camera-groups"`,
		`/api/cameras/${encodeURIComponent(camera.id)}/snapshot`,
		`cameraSettings.pendingAudio.set(group.id, input.checked)`,
		`adminFetch("/api/camera-groups/record-audio"`,
		`body: JSON.stringify({groups})`,
		`group.hasConnectedCameras &&`,
		`typeof pending === "boolean" &&`,
		`const applied = cameraSettings.currentAudio.get(group.id)`,
		`pending !== applied`,
		`elements.state.textContent = "待接入"`,
		`elements.state.textContent = "配置不一致"`,
		`rawRecordAudio === null ? null : Boolean(rawRecordAudio)`,
		`const verifiedAudio = await verifyCameraAudioSettings(payload, groups)`,
		`中断 15–60 秒`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("admin.js missing camera management behavior %q", content)
		}
	}

	refreshStart := strings.Index(script, "async function refresh()")
	if refreshStart < 0 {
		t.Fatal("admin.js missing device refresh function")
	}
	refreshEnd := strings.Index(script[refreshStart:], "\nfunction renderDevices")
	if refreshEnd < 0 {
		t.Fatal("admin.js missing bounded device refresh function")
	}
	refreshBody := script[refreshStart : refreshStart+refreshEnd]
	if strings.Contains(refreshBody, "loadCameraGroups") || strings.Contains(refreshBody, "/snapshot") {
		t.Fatal("30-second device refresh must not reload camera groups or snapshots")
	}
}

func TestAdminCameraGroupsKeepTwoViewsTogether(t *testing.T) {
	styleData, err := FS.ReadFile("assets/admin.css")
	if err != nil {
		t.Fatal(err)
	}
	style := string(styleData)
	for _, content := range []string{
		`.camera-groups { display: flex; flex-wrap: wrap;`,
		`.camera-group-views { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));`,
		`.camera-view.pending-slot`,
		`.camera-group.has-pending-change`,
	} {
		if !strings.Contains(style, content) {
			t.Fatalf("admin.css missing grouped camera layout %q", content)
		}
	}
}

func TestAdminCameraApplyResultReflectsRestartOutcome(t *testing.T) {
	scriptData, err := FS.ReadFile("assets/admin.js")
	if err != nil {
		t.Fatal(err)
	}
	script := string(scriptData)
	for _, content := range []string{
		`payload.restarted`,
		`Frigate 已恢复并确认录像状态。`,
		`设置一致，无需重启。`,
	} {
		if !strings.Contains(script, content) {
			t.Fatalf("admin.js missing restart-aware result %q", content)
		}
	}
	if strings.Contains(script, "设置已应用，Frigate 正在重启") {
		t.Fatal("admin.js must not describe a completed apply as still restarting")
	}
}
