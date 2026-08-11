function stringValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

export function normalizeCameraGroups(cameras, rawGroups) {
  const cameraList = Array.isArray(cameras) ? cameras : [];
  const hasGroupPayload = Array.isArray(rawGroups);
  const knownCameraIDs = new Set(
    cameraList.map((camera) => stringValue(camera?.id)).filter(Boolean),
  );
  const normalized = [];
  const seenGroupIDs = new Set();

  if (Array.isArray(rawGroups)) {
    for (const rawGroup of rawGroups) {
      const id = stringValue(rawGroup?.id);
      const name = stringValue(rawGroup?.name);
      if (!id || !name || seenGroupIDs.has(id)) continue;

      const cameraIDs = [];
      const seenCameraIDs = new Set();
      const candidates = Array.isArray(rawGroup?.camera_ids) ? rawGroup.camera_ids : [];
      for (const candidate of candidates) {
        const cameraID = stringValue(candidate);
        if (!knownCameraIDs.has(cameraID) || seenCameraIDs.has(cameraID)) continue;
        cameraIDs.push(cameraID);
        seenCameraIDs.add(cameraID);
        if (cameraIDs.length === 2) break;
      }
      if (!cameraIDs.length) continue;

      normalized.push({id, name, cameraIDs});
      seenGroupIDs.add(id);
    }
  }

  if (!normalized.length && !hasGroupPayload && cameraList.length) {
    normalized.push({
      id: "default",
      name: "摄像头",
      cameraIDs: [...knownCameraIDs].slice(0, 2),
    });
  }
  return normalized;
}

export function camerasForGroup(cameras, groups, groupID) {
  const group = (Array.isArray(groups) ? groups : []).find((item) => item.id === groupID);
  if (!group) return [];
  const camerasByID = new Map(
    (Array.isArray(cameras) ? cameras : []).map((camera) => [camera.id, camera]),
  );
  return group.cameraIDs
    .slice(0, 2)
    .map((cameraID) => camerasByID.get(cameraID))
    .filter(Boolean);
}

export async function transitionCameraGroup({
  currentGroupID,
  nextGroupID,
  groups,
  stopLive,
  abortRecordingIndex,
  stopReplay,
  muteReplay,
  activate,
  rebuild,
}) {
  const exists = (Array.isArray(groups) ? groups : []).some((group) => group.id === nextGroupID);
  if (!exists || nextGroupID === currentGroupID) return false;

  await stopLive();
  await abortRecordingIndex();
  await stopReplay();
  await muteReplay();
  await activate(nextGroupID);
  await rebuild();
  return true;
}
