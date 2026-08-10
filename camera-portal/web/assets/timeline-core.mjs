export const DAY_SECONDS = 24 * 60 * 60;
export const TIMELINE_WINDOW_SECONDS = 2 * DAY_SECONDS;
export const TIMELINE_SHIFT_SECONDS = DAY_SECONDS;
export const TIMELINE_EDGE_SECONDS = 2 * 60 * 60;

export function splitTimeRange(start, end, maximumSpan = DAY_SECONDS) {
  const rangeStart = Math.floor(Number(start));
  const rangeEnd = Math.floor(Number(end));
  const span = Math.floor(Number(maximumSpan));
  if (!Number.isFinite(rangeStart) || !Number.isFinite(rangeEnd) || rangeEnd <= rangeStart) {
    return [];
  }
  if (!Number.isFinite(span) || span <= 0) {
    throw new RangeError("maximumSpan must be positive");
  }

  const chunks = [];
  for (let cursor = rangeStart; cursor < rangeEnd;) {
    const chunkEnd = Math.min(rangeEnd, cursor + span);
    chunks.push({start: cursor, end: chunkEnd});
    cursor = chunkEnd;
  }
  return chunks;
}

export function clampTimestamp(value, minimum, maximum) {
  const timestamp = Number(value);
  const lower = Number(minimum);
  const upper = Number(maximum);
  if (!Number.isFinite(timestamp) || !Number.isFinite(lower) || !Number.isFinite(upper) || lower > upper) {
    throw new RangeError("invalid timestamp bounds");
  }
  return Math.max(lower, Math.min(upper, timestamp));
}

export function rebaseRollingWindow({
  windowStart,
  selectedTimestamp,
  minWindowStart,
  maxWindowStart,
  windowSeconds = TIMELINE_WINDOW_SECONDS,
  shiftSeconds = TIMELINE_SHIFT_SECONDS,
  edgeSeconds = TIMELINE_EDGE_SECONDS,
}) {
  let start = Number(windowStart);
  const selected = Number(selectedTimestamp);
  const minimum = Number(minWindowStart);
  const maximum = Number(maxWindowStart);
  if (
    ![start, selected, minimum, maximum, windowSeconds, shiftSeconds, edgeSeconds].every(Number.isFinite) ||
    minimum > maximum ||
    windowSeconds <= 0 ||
    shiftSeconds <= 0 ||
    edgeSeconds < 0 ||
    edgeSeconds * 2 >= windowSeconds
  ) {
    throw new RangeError("invalid rolling timeline window");
  }

  start = Math.max(minimum, Math.min(maximum, start));
  while (selected < start + edgeSeconds && start > minimum) {
    const shifted = Math.max(minimum, start - shiftSeconds);
    if (shifted === start) break;
    start = shifted;
  }
  while (selected > start + windowSeconds - edgeSeconds && start < maximum) {
    const shifted = Math.min(maximum, start + shiftSeconds);
    if (shifted === start) break;
    start = shifted;
  }
  return start;
}
