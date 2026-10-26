const DAY_MINUTES = 24 * 60;

export function timeRange(time: string): { start: number; end: number } | null {
  const matches = [...time.matchAll(/(?<![\d:])(24:00|(?:2[0-3]|[01]?\d)(?::[0-5]\d)?)(?![\d:])/gu)];
  const minutes = matches.map(match => {
    const [hours, minutes = 0] = match[1].split(':').map(Number);
    return hours * 60 + minutes;
  });
  if (!minutes.length) {
    for (const [expression, hour] of [[/凌晨|清晨/u, 4], [/早上|上午/u, 8], [/中午/u, 12], [/下午/u, 14], [/傍晚/u, 18], [/晚上|夜间|深夜/u, 20]] as const) {
      if (expression.test(time)) return { start: hour * 60, end: (hour + 2) * 60 };
    }
    return null;
  }
  const start = minutes[0];
  let end = minutes.length === 1 ? start + 120 : minutes[1];
  if (end <= start) end += DAY_MINUTES;
  return { start, end };
}

export function itemOccupies(item: { date: string; time: string }, date: string, slot: number) {
  const range = timeRange(item.time);
  if (!range) return false;
  // Calendar dates are compared in UTC to avoid DST changing the day length.
  const offset = (Date.parse(`${date}T00:00:00Z`) - Date.parse(`${item.date}T00:00:00Z`)) / 86_400_000;
  const slotStart = offset * DAY_MINUTES + slot * 120;
  return slotStart < range.end && slotStart + 120 > range.start;
}
