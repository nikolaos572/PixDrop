"use strict";

// The read-only server info page; common.js provides $, api, el, showMessage, the
// formatting helpers and the header.

function duration(seconds) {
  return seconds % 60 === 0 ? plural(seconds / 60, "minute") : plural(seconds, "second");
}

// The address to give an AI agent: the server's public one (PUBLIC_BASE_URL, or else the
// address this browser used). The page never knows the token: the command has a placeholder.
function showMcp(server) {
  const on = Boolean(server.mcp_url);
  $("mcp").replaceChildren(on ? el("code", {}, server.mcp_url) : "Off (ENABLE_MCP=1 turns it on)");
  const lifetime = duration(server.link_expiry_minutes * 60);
  $("link-expiry").textContent = on ? `Valid for ${lifetime}, for one transfer` : `${lifetime} (MCP is off)`;
  $("mcp-setup").hidden = !on;
  $("mcp-command").textContent = on
    ? `claude mcp add --transport http --scope user pixdrop ${server.mcp_url} --header "Authorization: Bearer <your token>"`
    : "";
}

function showServer(server) {
  $("version").textContent = server.version;
  $("default-model").textContent = server.default_model ?? "None";
  $("models").textContent = formatNumber(server.models);
  $("timeout").textContent = `${duration(server.generation_timeout_seconds)}, then the job fails`;
  $("max-upload").textContent = `${server.max_upload_mb} MB per reference image`;
  $("max-references").textContent = `At most ${server.max_reference_images} per job (fewer for some models)`;
  $("max-active").textContent = `${server.max_active_jobs} at a time (pending or generating)`;
  $("medium-size").textContent = `${server.medium_image_px} px on the longest side`;
  $("max-prompt").textContent = plural(server.max_prompt_chars, "character");
  $("timezone").textContent = server.timezone;
  showMcp(server);
}

function showAccount(account) {
  $("account-user").textContent = account.user;
  $("account-jobs").textContent = formatNumber(account.jobs);
  $("account-active").textContent = formatNumber(account.in_progress);
  $("storage").textContent = formatSize(account.storage_bytes);
}

function optionText(option) {
  return option.default === null ? option.name : `${option.name} (default ${option.default})`;
}

function modelRow(model) {
  const name = el("td", { className: "nowrap" }, el("code", {}, model.name));
  if (model.default) name.append(" ", el("span", { className: "badge default" }, "default"));
  return el(
    "tr",
    {},
    name,
    el("td", {}, model.description || "–"),
    el("td", { className: "nowrap" }, model.api_type),
    el("td", {}, el("code", {}, model.upstream_model)),
    el("td", {}, optionsText(model)),
    el("td", { className: "number" }, referencesText(model)),
  );
}

// The options, and where they come from: config.json, or the provider's model catalogue (read in the background).
function optionsText(model) {
  if (model.options_from === "pending") return "Not known yet: the provider's catalogue has not been read";
  const options = model.options.map(optionText).join(", ") || "None";
  return model.options_from === "provider" ? `${options} (from the provider's catalogue)` : options;
}

function referencesText(model) {
  const { min_reference_images: least, max_reference_images: most } = model;
  if (!most) return "None";
  return least ? `${least} to ${most}` : String(most);
}

function showModels(models) {
  $("models-body").replaceChildren(...models.map(modelRow));
  $("models-table").hidden = models.length === 0;
  $("no-models").hidden = models.length > 0;
}

async function loadInfo() {
  try {
    const [{ server, account }, { models }] = await Promise.all([api("/api/info"), api("/api/models")]);
    showServer(server);
    showAccount(account);
    showModels(models);
    $("message").hidden = true;
  } catch (error) {
    showMessage(error.message, "error");
    $("message").hidden = false;
  }
}

async function copyCommand() {
  const button = $("copy-command");
  try {
    await copyText($("mcp-command").textContent);
    button.textContent = "Copied";
    setTimeout(() => {
      button.textContent = "Copy command";
    }, 1500);
  } catch (error) {
    showMessage(`Copying failed: ${error.message}`, "error");
    $("message").hidden = false;
  }
}

$("refresh").addEventListener("click", loadInfo);
$("copy-command").addEventListener("click", copyCommand);
loadInfo();
