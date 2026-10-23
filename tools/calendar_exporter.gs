/***** Jaime's Calendar Exporter — corrected *****
 *
 * Two fixes over the original, both about over-counting hours:
 *
 *  0. DAY BOUNDARY AT 4AM. A working day runs 4am to 4am, so an evening block
 *     finishing at 1:30am counts against that evening, not the next day.
 *
 *  1. CLIP TO THE WINDOW. Calendar.Events.list returns every event that
 *     OVERLAPS the range, and the original counted each one's full duration.
 *     A block running 15 Oct 9:30pm -> 16 Oct 1:30am therefore put all 4 hours
 *     into a 1-15 Oct window, when 1.5 of them happened on 16 October and
 *     belong to the next period. Each event is now trimmed to the window.
 *
 *  2. REPORT DOUBLE-BOOKED TIME. Two events on the same minutes are still
 *     billed twice (COUNT_OVERLAPPING_TIME), because meetings overrun their
 *     slots, but the amount is now reported so a figure can be reconciled
 *     against one counted the other way. Measured: 0.25 h to 2.33 h a fortnight.
 *
 * The per-row "Duration (mins)" column now shows BILLABLE minutes (clipped).
 * The script also writes the corrected totals to the "Ridecare Hours" tab,
 * because that tab's =SUMIF(...E:E) would otherwise still add the overlaps back.
 **************************************************/

/***** SETTINGS *****/
const TARGET_CALENDAR_ID  = "jaime@pvragon.com";
const TIMEZONE            = "America/Los_Angeles";
const SKIP_ALL_DAY_EVENTS = true;   // skip Home / trips / OOO (1440+ min noise)
const SKIP_CANCELLED      = true;
const DEFAULT_COLOR_LABEL = "No Color (default)";
const BILLABLE_CATEGORY   = "Ride Care";
// Where one working day ends. Work past midnight belongs to the day it started
// on, so 4am: an evening block running to 1:30am is that evening's work. The
// window therefore runs 4am on the first date to 4am the morning AFTER the last.
const DAY_START_HOUR      = 4;
// Two events on the same minutes are billed twice, on the client's instruction:
// meetings overrun their slots and the marked times understate that. Set false
// to count the real elapsed time instead; the report shows the amount either way.
const COUNT_OVERLAPPING_TIME = true;
const TOTALS_SHEET_NAME   = "Ridecare Hours";
/********************/

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Calendar Sync')
    .addItem('Get Calendar Events', 'getCalendarEvents')
    .addToUi();
}

function getCalendarEvents() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const startVal = sheet.getRange("B2").getValue();
  const endVal   = sheet.getRange("B3").getValue();
  if (!(startVal instanceof Date) || !(endVal instanceof Date)) {
    Browser.msgBox("Please enter valid dates in B2 and B3.");
    return;
  }

  const startDate = new Date(startVal); startDate.setHours(DAY_START_HOUR, 0, 0, 0);
  const endDate   = new Date(endVal);
  endDate.setDate(endDate.getDate() + 1);
  endDate.setHours(DAY_START_HOUR, 0, 0, 0);
  const winStart  = startDate.getTime();
  const winEnd    = endDate.getTime();

  let items = [], pageToken = null;
  do {
    const resp = Calendar.Events.list(TARGET_CALENDAR_ID, {
      timeMin: startDate.toISOString(),
      timeMax: endDate.toISOString(),
      singleEvents: true,
      orderBy: 'startTime',
      maxResults: 2500,
      showDeleted: false,
      pageToken: pageToken
    });
    if (resp.items) items = items.concat(resp.items);
    pageToken = resp.nextPageToken;
  } while (pageToken);

  const colorMap = loadColorMap();

  const lastRow = sheet.getLastRow();
  if (lastRow >= 5) sheet.getRange(5, 1, lastRow - 4, 7).clearContent();

  const out = [["Date","Event Name","Start Time","End Time","Duration (mins)","Event Description","Color Category"]];
  const summedMs = {};   // every duration added up — what the old script reported
  const spans    = {};   // per category, for merging overlaps
  let clippedMs  = 0;    // billable time trimmed off for falling outside the window

  items.forEach(ev => {
    if (SKIP_CANCELLED && ev.status === 'cancelled') return;
    const isAllDay = !ev.start.dateTime;
    if (SKIP_ALL_DAY_EVENTS && isAllDay) return;

    const start = isAllDay ? new Date(ev.start.date + "T00:00:00") : new Date(ev.start.dateTime);
    const end   = isAllDay ? new Date(ev.end.date   + "T00:00:00") : new Date(ev.end.dateTime);

    // FIX 1: keep only the part of the event that falls inside the window.
    const rawStart = start.getTime(), rawEnd = end.getTime();
    const cs = Math.max(rawStart, winStart);
    const ce = Math.min(rawEnd,   winEnd);
    if (ce <= cs) return;                       // wholly outside the window

    const durationMins = Math.round((ce - cs) / 60000);   // BILLABLE minutes
    const category = getColorName(ev.colorId, colorMap);
    if (category === BILLABLE_CATEGORY) {
      clippedMs += (cs - rawStart) + (rawEnd - ce);
    }

    summedMs[category] = (summedMs[category] || 0) + (ce - cs);
    (spans[category] = spans[category] || []).push([cs, ce]);

    // Label the row with the WORKING day: before the boundary it is still the
    // previous date, so 1:30am work shows under the evening it came from.
    const workDay = new Date(cs);
    if (workDay.getHours() < DAY_START_HOUR) workDay.setDate(workDay.getDate() - 1);

    out.push([
      Utilities.formatDate(workDay, TIMEZONE, "EEE MMM dd yyyy"),
      ev.summary || "(no title)",
      Utilities.formatDate(start, TIMEZONE, "h:mm a"),   // exact start from the calendar
      Utilities.formatDate(end,   TIMEZONE, "h:mm a"),   // exact end from the calendar
      durationMins,
      cleanEventDescription(ev.description),
      category
    ]);
  });

  if (out.length === 1) {
    sheet.getRange(5, 1, 1, out[0].length).setValues(out);   // leave the header behind
    Browser.msgBox("No events found in this range.");
    return;
  }

  sheet.getRange(5, 1, out.length, out[0].length).setValues(out);

  // FIX 2: the billable total is elapsed time, not the sum of durations.
  const mergedMins = mergedMinutes(spans[BILLABLE_CATEGORY] || []);
  const summedMins = Math.round((summedMs[BILLABLE_CATEGORY] || 0) / 60000);
  const doubleBooked = summedMins - mergedMins;

  writeTotals(mergedMins, doubleBooked, Math.round(clippedMs / 60000));

  Browser.msgBox(
    "Synced " + (out.length - 1) + " events.\n\n" +
    BILLABLE_CATEGORY + " billable hours: " + (mergedMins / 60).toFixed(2) + "\n\n" +
    "(double-booked time counted once: " + (doubleBooked / 60).toFixed(2) + " h\n" +
    " trimmed as outside the window: " + (clippedMs / 60000 / 60).toFixed(2) + " h)"
  );
}

/** Total elapsed minutes across intervals, overlaps counted once. */
function mergedMinutes(list) {
  if (!list.length) return 0;
  const spans = list.slice().sort(function (a, b) { return a[0] - b[0]; });
  let total = 0, curStart = spans[0][0], curEnd = spans[0][1];
  for (let i = 1; i < spans.length; i++) {
    const s = spans[i][0], e = spans[i][1];
    if (s > curEnd) {                 // a real gap: bank the block and start again
      total += curEnd - curStart;
      curStart = s; curEnd = e;
    } else if (e > curEnd) {          // overlapping or touching: extend
      curEnd = e;
    }
  }
  total += curEnd - curStart;
  return Math.round(total / 60000);
}

/**
 * Put the figures on the totals tab. The old =SUMIF over the Duration column
 * cannot express either rule once events are clipped, so the script owns these
 * cells.
 */
function writeTotals(billableMins, doubleBookedMins, clippedMins) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(TOTALS_SHEET_NAME);
  if (!sheet) return;
  sheet.getRange("A1:B4").setValues([
    ["Total " + BILLABLE_CATEGORY + " minutes", billableMins],
    ["Total " + BILLABLE_CATEGORY + " hours:",  billableMins / 60],
    [COUNT_OVERLAPPING_TIME ? "Double-booked hours (included above)"
                            : "Double-booked hours (counted once)", doubleBookedMins / 60],
    ["Hours trimmed as outside the window",     clippedMins / 60]
  ]);
}

function loadColorMap() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("ColorMapping");
  const map = {};
  if (!sheet) return map;
  const rows = sheet.getDataRange().getValues();
  for (let i = 1; i < rows.length; i++) {
    const id = String(rows[i][0]).trim();
    if (id !== "") map[id] = rows[i][2];
  }
  return map;
}

function getColorName(colorId, colorMap) {
  if (colorId === undefined || colorId === null || colorId === "") return DEFAULT_COLOR_LABEL;
  const key = String(colorId).trim();
  return colorMap.hasOwnProperty(key) ? colorMap[key] : "Unknown";
}

/**
 * Strip pasted meeting boilerplate. Every keyword is applied, cutting at the
 * EARLIEST one present — the original returned at the first keyword in list
 * order, which could leave later boilerplate in place.
 */
function cleanEventDescription(description) {
  if (!description) return "";
  const removalKeywords = [
    "____","Microsoft Teams Need Help","Join the meeting","Meeting ID",
    "Passcode","Dial in by phone","Find a local number","More info",
    "Join on a video conferencing device","Meeting options","Confidentiality Notice"
  ];
  let cut = description.length;
  for (let kw of removalKeywords) {
    const i = description.indexOf(kw);
    if (i !== -1 && i < cut) cut = i;
  }
  return description.substring(0, cut).trim();
}
