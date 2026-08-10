import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";

import {createReplayAudioController} from "../assets/client.js";

function createHarness() {
  const players = new Map([
    ["yard", {muted: true}],
    ["door", {muted: true}],
  ]);
  let fullscreenCameraID = null;
  const changes = [];
  const controller = createReplayAudioController({
    players,
    isEligible: (cameraID) => cameraID === fullscreenCameraID,
    onChange: (cameraID) => changes.push(cameraID),
  });

  return {
    controller,
    players,
    changes,
    setFullscreen(cameraID) {
      fullscreenCameraID = cameraID;
    },
  };
}

test("keeps replay muted until the current fullscreen camera is explicitly enabled", () => {
  const harness = createHarness();

  assert.equal(harness.controller.toggle("yard"), false);
  assert.equal(harness.controller.audibleCameraID, null);
  assert.equal(harness.players.get("yard").muted, true);
  assert.equal(harness.players.get("door").muted, true);

  harness.setFullscreen("yard");
  assert.equal(harness.controller.toggle("yard"), true);
  assert.equal(harness.controller.audibleCameraID, "yard");
  assert.equal(harness.players.get("yard").muted, false);
  assert.equal(harness.players.get("door").muted, true);
});

test("allows at most one audible replay when the fullscreen camera changes", () => {
  const harness = createHarness();
  harness.setFullscreen("yard");
  harness.controller.toggle("yard");

  harness.setFullscreen("door");
  assert.equal(harness.controller.toggle("door"), true);
  assert.equal(harness.controller.audibleCameraID, "door");
  assert.equal(harness.players.get("yard").muted, true);
  assert.equal(harness.players.get("door").muted, false);
});

test("immediately mutes replay after fullscreen eligibility is lost", () => {
  const harness = createHarness();
  harness.setFullscreen("yard");
  harness.controller.toggle("yard");

  harness.setFullscreen(null);
  harness.controller.reconcile();

  assert.equal(harness.controller.audibleCameraID, null);
  assert.equal(harness.players.get("yard").muted, true);
  assert.equal(harness.players.get("door").muted, true);
  assert.equal(harness.changes.at(-1), null);
});

test("muteAll covers mode, date, camera and playback lifecycle resets", () => {
  const harness = createHarness();
  harness.setFullscreen("yard");
  harness.controller.toggle("yard");

  harness.controller.muteAll();

  assert.equal(harness.controller.audibleCameraID, null);
  assert.equal(harness.players.get("yard").muted, true);
  assert.equal(harness.players.get("door").muted, true);
});

test("does not require an audioTracks API or an audio track to keep video usable", () => {
  const harness = createHarness();
  harness.setFullscreen("yard");

  assert.doesNotThrow(() => harness.controller.toggle("yard"));
  assert.equal(harness.players.get("yard").muted, false);
});

test("reconcile remutes an externally unmuted non-selected player", () => {
  const harness = createHarness();
  harness.setFullscreen("yard");
  harness.controller.toggle("yard");
  harness.players.get("door").muted = false;

  harness.controller.reconcile();

  assert.equal(harness.players.get("yard").muted, false);
  assert.equal(harness.players.get("door").muted, true);
  assert.equal(harness.controller.audibleCameraID, "yard");
});

test("wires immediate muting into fullscreen, date, camera and mode transitions", async () => {
  const source = await readFile(new URL("../assets/client.js", import.meta.url), "utf8");

  assert.match(source, /fullscreenchange[\s\S]*?replayAudio\.reconcile\(\)/);
  assert.match(source, /replayDate\.addEventListener\("change"[\s\S]*?replayAudio\.muteAll\(\)/);
  assert.match(source, /function focusReplayCamera[\s\S]*?replayAudio\.muteAll\(\)/);
  assert.match(source, /async function switchMode[\s\S]*?replayAudio\.muteAll\(\)/);
  assert.match(source, /function stopReplay[\s\S]*?video\.muted = true/);
});

test("does not add the replay audio control to the live-player construction path", async () => {
  const source = await readFile(new URL("../assets/client.js", import.meta.url), "utf8");
  const liveBlock = source.slice(
    source.indexOf("function createLiveCard"),
    source.indexOf("function ensureVideoRTC"),
  );

  assert.doesNotMatch(liveBlock, /addReplayAudioToggle/);
});

test("tries landscape lock for the replay CSS fallback without forcing native video fullscreen", async () => {
  const source = await readFile(new URL("../assets/client.js", import.meta.url), "utf8");
  const enterFullscreenStart = source.indexOf("function enterFullscreen");
  const replayBranchStart = source.indexOf('if (state.mode === "replay") {', enterFullscreenStart);
  const nativeVideoBranchStart = source.indexOf("} else if (video?.webkitEnterFullscreen)", replayBranchStart);
  const replayBranch = source.slice(replayBranchStart, nativeVideoBranchStart);

  assert.match(
    source,
    /const useFocusedFallback = \(\) => \{[\s\S]*?const focused = card\.classList\.toggle\("focused"\);[\s\S]*?if \(focused && state\.mode === "replay"\) lockLandscape\(\)/,
  );
  assert.doesNotMatch(replayBranch, /webkitEnterFullscreen/);
  assert.match(source, /button\.className = "replay-audio-toggle"/);
});
