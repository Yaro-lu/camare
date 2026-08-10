import assert from "node:assert/strict";
import test from "node:test";

import {
  DAY_SECONDS,
  TIMELINE_WINDOW_SECONDS,
  clampTimestamp,
  rebaseRollingWindow,
  splitTimeRange,
} from "../assets/timeline-core.mjs";

test("splits a full 48-hour index window into two valid portal requests", () => {
  const start = 1_800_000_000;
  const chunks = splitTimeRange(start, start + TIMELINE_WINDOW_SECONDS);

  assert.deepEqual(chunks, [
    {start, end: start + DAY_SECONDS},
    {start: start + DAY_SECONDS, end: start + 2 * DAY_SECONDS},
  ]);
  assert.ok(chunks.every((chunk) => chunk.end - chunk.start <= DAY_SECONDS));
});

test("keeps a partial current-day request contiguous and below 24 hours", () => {
  const start = 1_800_000_000;
  const end = start + DAY_SECONDS + 3_605;
  const chunks = splitTimeRange(start, end);

  assert.deepEqual(chunks, [
    {start, end: start + DAY_SECONDS},
    {start: start + DAY_SECONDS, end},
  ]);
});

test("does not rebase merely because the selection crosses midnight", () => {
  const dayStart = 1_800_000_000;
  const windowStart = dayStart - DAY_SECONDS;
  const fiveMinutesAfterMidnight = dayStart + 5 * 60;
  const fiveMinutesBeforeMidnight = dayStart - 5 * 60;

  assert.equal(rebaseRollingWindow({
    windowStart,
    selectedTimestamp: fiveMinutesAfterMidnight,
    minWindowStart: windowStart - 12 * DAY_SECONDS,
    maxWindowStart: windowStart,
  }), windowStart);
  assert.equal(rebaseRollingWindow({
    windowStart,
    selectedTimestamp: fiveMinutesBeforeMidnight,
    minWindowStart: windowStart - 12 * DAY_SECONDS,
    maxWindowStart: windowStart,
  }), windowStart);
});

test("rebases backward by one day near the left edge without changing the timestamp", () => {
  const windowStart = 1_800_000_000;
  const selectedTimestamp = windowStart + 30 * 60;

  const rebased = rebaseRollingWindow({
    windowStart,
    selectedTimestamp,
    minWindowStart: windowStart - 12 * DAY_SECONDS,
    maxWindowStart: windowStart + DAY_SECONDS,
  });

  assert.equal(rebased, windowStart - DAY_SECONDS);
  assert.equal(selectedTimestamp, 1_800_001_800);
});

test("rebases forward by one day near the right edge", () => {
  const windowStart = 1_800_000_000;
  const selectedTimestamp = windowStart + TIMELINE_WINDOW_SECONDS - 30 * 60;

  assert.equal(rebaseRollingWindow({
    windowStart,
    selectedTimestamp,
    minWindowStart: windowStart - DAY_SECONDS,
    maxWindowStart: windowStart + 4 * DAY_SECONDS,
  }), windowStart + DAY_SECONDS);
});

test("respects the oldest and newest permitted rolling windows", () => {
  const oldest = 1_800_000_000;
  const newest = oldest + 12 * DAY_SECONDS;

  assert.equal(rebaseRollingWindow({
    windowStart: oldest,
    selectedTimestamp: oldest + 60,
    minWindowStart: oldest,
    maxWindowStart: newest,
  }), oldest);
  assert.equal(rebaseRollingWindow({
    windowStart: newest,
    selectedTimestamp: newest + TIMELINE_WINDOW_SECONDS - 60,
    minWindowStart: oldest,
    maxWindowStart: newest,
  }), newest);
});

test("clamps selections to the 14-day history and current-time bounds", () => {
  const now = 1_800_000_000;
  const earliest = now - 14 * DAY_SECONDS;

  assert.equal(clampTimestamp(earliest - 1, earliest, now), earliest);
  assert.equal(clampTimestamp(now + 1, earliest, now), now);
  assert.equal(clampTimestamp(now - 123, earliest, now), now - 123);
});
