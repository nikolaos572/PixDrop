"use strict";

// Your statistics, computed by the server from all your jobs; common.js provides $, api,
// el, showMessage, the formatting helpers and the header.

const SOURCE_NAMES = { web: "Web page", api: "API", mcp: "MCP (AI agents)" };
const monthFormat = new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" });

// "2026-11" (a month in the server's time zone) as "November 2026".
function monthName(month) {
  const [year, number] = month.split("-").map(Number);
  return monthFormat.format(Date.UTC(year, number - 1, 1));
}

function averageTokens(totals) {
  return totals.average_tokens === null ? "–" : formatNumber(Math.round(totals.average_tokens));
}

function tile(label, value, ...lines) {
  return el(
    "div",
    { className: "tile" },
    el("dt", {}, label),
    el("dd", { className: "value" }, value),
    ...lines.map((line) => el("dd", { className: "sub" }, line)),
  );
}

function renderTiles(totals) {
  const cost = totals.average_cost_usd === null
    ? "No cost reported"
    : `${formatCost(totals.average_cost_usd)} per job on average`;
  const tokens = totals.average_tokens === null
    ? "No tokens reported"
    : `${averageTokens(totals)} per job on average`;
  const split = `${formatNumber(totals.prompt_tokens)} prompt · ${formatNumber(totals.completion_tokens)} completion`;
  $("tiles").replaceChildren(
    tile("Jobs", formatNumber(totals.jobs), `${formatNumber(totals.completed)} completed · ${formatNumber(totals.failed)} failed`),
    tile("In progress", formatNumber(totals.in_progress), "Pending or generating"),
    tile("Images generated", formatNumber(totals.images), `${plural(totals.references, "reference image")} uploaded`),
    tile("Total cost", formatCost(totals.cost_usd), cost),
    tile("Tokens", formatNumber(totals.total_tokens), tokens, split),
    tile("Average duration", formatDuration(totals.average_duration_seconds), "Of a completed job"),
  );
}

function renderSources(sources, jobs) {
  const entries = Object.entries(sources).sort(([, a], [, b]) => b - a);
  $("sources").replaceChildren(
    ...entries.flatMap(([source, count]) => [
      el("dt", {}, SOURCE_NAMES[source] ?? source),
      el("dd", {}, `${plural(count, "job")} (${Math.round((100 * count) / jobs)}%)`),
    ]),
  );
}

function tableRow(label, totals) {
  const values = [
    formatNumber(totals.jobs),
    formatNumber(totals.completed),
    formatNumber(totals.failed),
    formatNumber(totals.images),
    formatCost(totals.cost_usd),
    formatCost(totals.average_cost_usd),
    formatNumber(totals.total_tokens),
    formatDuration(totals.average_duration_seconds),
  ];
  return el("tr", {}, el("td", {}, label), ...values.map((value) => el("td", { className: "number" }, value)));
}

async function loadStats() {
  try {
    const stats = await api("/api/stats");
    const empty = stats.totals.jobs === 0;
    $("no-jobs").hidden = !empty;
    for (const id of ["summary", "by-model", "by-month"]) $(id).hidden = empty;
    if (!empty) {
      renderTiles(stats.totals);
      $("first-job").textContent = formatTime(stats.first_job_at);
      $("last-job").textContent = formatTime(stats.last_job_at);
      renderSources(stats.sources, stats.totals.jobs);
      $("models-body").replaceChildren(...stats.models.map((line) => tableRow(line.model, line)));
      $("months-body").replaceChildren(...stats.months.map((line) => tableRow(monthName(line.month), line)));
    }
    $("message").hidden = true;
  } catch (error) {
    showMessage(error.message, "error");
    $("message").hidden = false;
  }
}

$("refresh").addEventListener("click", loadStats);
loadStats();
