import assert from "node:assert/strict";
import test from "node:test";

import {
  camerasForGroup,
  normalizeCameraGroups,
  transitionCameraGroup,
} from "../assets/camera-groups.mjs";

const cameras = [
  {id: "ptz_view", name: "云台"},
  {id: "fixed_view", name: "枪机"},
  {id: "front_ptz_view", name: "门前云台"},
  {id: "front_fixed_view", name: "门前固定"},
];

test("empty groups are hidden and each public group exposes at most two known cameras", () => {
  const groups = normalizeCameraGroups(cameras, [
    {id: "yard", name: "院子", camera_ids: ["ptz_view", "fixed_view", "front_ptz_view"]},
    {id: "reserved", name: "预留", camera_ids: []},
    {id: "front", name: "门前", camera_ids: ["front_ptz_view", "missing", "front_fixed_view"]},
  ]);

  assert.deepEqual(groups, [
    {id: "yard", name: "院子", cameraIDs: ["ptz_view", "fixed_view"]},
    {id: "front", name: "门前", cameraIDs: ["front_ptz_view", "front_fixed_view"]},
  ]);
  assert.deepEqual(camerasForGroup(cameras, groups, "front"), [
    cameras[2],
    cameras[3],
  ]);
});

test("legacy payload without groups remains a single hidden-tab two-camera group", () => {
  const groups = normalizeCameraGroups(cameras.slice(0, 2), undefined);

  assert.deepEqual(groups, [
    {id: "default", name: "摄像头", cameraIDs: ["ptz_view", "fixed_view"]},
  ]);
  assert.deepEqual(camerasForGroup(cameras, groups, "unknown"), []);
});

test("a payload containing only empty groups displays no camera group", () => {
  assert.deepEqual(normalizeCameraGroups(cameras, [
    {id: "reserved", name: "预留", camera_ids: []},
  ]), []);
});

test("camera group transition tears down old resources before activating and rebuilding", async () => {
  const calls = [];
  const changed = await transitionCameraGroup({
    currentGroupID: "yard",
    nextGroupID: "front",
    groups: normalizeCameraGroups(cameras, [
      {id: "yard", name: "院子", camera_ids: ["ptz_view", "fixed_view"]},
      {id: "front", name: "门前", camera_ids: ["front_ptz_view", "front_fixed_view"]},
    ]),
    stopLive: () => calls.push("stop-live"),
    abortRecordingIndex: () => calls.push("abort-recording-index"),
    stopReplay: () => calls.push("stop-replay"),
    muteReplay: () => calls.push("mute-replay"),
    activate: (groupID) => calls.push(`activate:${groupID}`),
    rebuild: async () => calls.push("rebuild"),
  });

  assert.equal(changed, true);
  assert.deepEqual(calls, [
    "stop-live",
    "abort-recording-index",
    "stop-replay",
    "mute-replay",
    "activate:front",
    "rebuild",
  ]);
});

test("camera group transition ignores the current or an unknown group", async () => {
  const calls = [];
  const groups = normalizeCameraGroups(cameras, [
    {id: "yard", name: "院子", camera_ids: ["ptz_view", "fixed_view"]},
  ]);
  const options = {
    currentGroupID: "yard",
    groups,
    stopLive: () => calls.push("stop-live"),
    abortRecordingIndex: () => calls.push("abort-recording-index"),
    stopReplay: () => calls.push("stop-replay"),
    muteReplay: () => calls.push("mute-replay"),
    activate: () => calls.push("activate"),
    rebuild: () => calls.push("rebuild"),
  };

  assert.equal(await transitionCameraGroup({...options, nextGroupID: "yard"}), false);
  assert.equal(await transitionCameraGroup({...options, nextGroupID: "missing"}), false);
  assert.deepEqual(calls, []);
});
