"use strict";

// Shared by the pages behind the login, loaded before their own script: API calls, the
// header, and the formatting of numbers, sizes, times and costs.

const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("en");

// Every request carries the cookie; the extra header marks it as coming from this page
// (the server refuses cookie-authenticated changes without it). Returns the response.
async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { ...options.headers, "X-Requested-With": "fetch" } });
  } catch {
    throw new Error("The server cannot be reached.");
  }
  if (response.status === 401) {
    location.reload(); // token revoked or cookie expired: back to the login page
    throw new Error("Session expired.");
  }
  if (!response.ok) throw new Error(await errorDetail(response));
  return response;
}

async function api(path, options = {}) {
  const response = await request(path, options);
  return response.status === 204 ? null : response.json();
}

// The server's reason ({"detail": ...}), or the status if the body has none.
async function errorDetail(response) {
  try {
    const { detail } = await response.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((item) => item.msg ?? JSON.stringify(item)).join("; ");
    return JSON.stringify(detail);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function showMessage(text, kind = "", element = $("message")) {
  element.textContent = text;
  element.className = `message ${kind}`;
}

// An element with attributes (or properties, for those not written as attributes) and children.
function el(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "className" || name === "textContent" || name === "hidden") element[name] = value;
    else element.setAttribute(name, value === true ? "" : String(value));
  }
  element.append(...children.filter((child) => child !== null && child !== undefined));
  return element;
}

function plural(count, word, words = `${word}s`) {
  return `${numberFormat.format(count)} ${count === 1 ? word : words}`;
}

function formatNumber(value) {
  return value === null || value === undefined ? "–" : numberFormat.format(value);
}

function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

// Dates come in the server's time zone (TZ), as ISO 8601: shown as they are, without the offset.
function formatTime(iso) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "";
}

// In US dollars, as the provider reports it: four decimals, since one image costs a few cents.
function formatCost(usd) {
  return usd === null || usd === undefined ? "–" : `$${usd.toFixed(4)}`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds - minutes * 60)} s`;
}

// The Clipboard API needs a secure context (HTTPS or localhost); a server reached over plain
// HTTP on the network gets the older way, through a selected, invisible text area. That one
// goes into the open dialog, if any: the rest of the page is inert while a dialog is open.
async function copyText(text) {
  if (navigator.clipboard && isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const focused = document.activeElement;
  const area = el("textarea", { readonly: true, "aria-hidden": "true", className: "offscreen" });
  area.value = text;
  (document.querySelector("dialog[open]") ?? document.body).append(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  focused?.focus();
  if (!copied) throw new Error("The browser refused to copy.");
}

async function logout() {
  await api("/api/session", { method: "DELETE" });
  location.reload();
}

$("logout").addEventListener("click", logout);
api("/api/session").then((session) => {
  $("user").textContent = session.user;
});
