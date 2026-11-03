"use strict";

// The generate page: the form, the history grid and the job dialog. common.js provides $,
// api, request, el, showMessage and the formatting helpers, and runs the header.
//
// Images are always loaded from this page's own server (relative URLs), never from the
// absolute URLs in the jobs: those are built from PUBLIC_BASE_URL, which may be another
// address, and the cookie that authenticates the images only goes to this one.

const PAGE_SIZE = 12;
const JOB_ID = /^\d{13}_[0-9a-f]{4}$/;
const SVG_NS = "http://www.w3.org/2000/svg";
const IMAGE_TYPES = {
  "image/png": "PNG",
  "image/jpeg": "JPEG",
  "image/webp": "WebP",
  "image/gif": "GIF",
  "image/svg+xml": "SVG",
};
// For files whose type the browser does not know (the server recognises images from their content).
const EXTENSION_TYPES = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
  svg: "image/svg+xml",
};
const OPTION_LABELS = {
  aspect_ratio: "Aspect ratio",
  resolution: "Resolution",
  size: "Size",
  quality: "Quality",
  background: "Background",
  output_format: "Format",
  output_compression: "Compression",
  n: "Images",
  seed: "Seed",
};
const OPTION_EXAMPLES = { size: "e.g. 1024x1024" };
const SOURCES = { web: "Web page", api: "API", mcp: "MCP (AI agent)" };
const PLACEHOLDERS = {
  pending: ["icon-clock", "Waiting for reference images"],
  generating: ["icon-sparkle", "Generating…"],
  failed: ["icon-alert", "Failed"],
  completed: ["icon-picture", "No image"],
};

const state = {
  models: [], // GET /api/models
  maxUploadMb: 20, // until GET /api/info tells
  references: [], // {file, url}: the reference images chosen for the next job
  optionValues: {}, // option name -> value chosen ("" for Default), kept when the model changes
  submitting: false,
  page: 1,
  loads: 0,
  rows: null, // the JSON of the jobs shown, to re-render only when it changes
  jobs: [],
  refreshTimer: null,
  dialogJob: null, // the job shown in the dialog
  dialogJson: null,
  opener: null, // what gets the focus back when the dialog closes
  focusAfterClose: null,
  pointerDownOnBackdrop: false,
};
const shownError = { hint: null, hideTimer: null }; // the "?" whose error is open

function imageUrl(jobId, index, size = "full") {
  return `/api/jobs/${jobId}/images/${index}${size === "medium" ? "?size=medium" : ""}`;
}

function referenceUrl(jobId, index, size = "full") {
  return `/api/jobs/${jobId}/references/${index}${size === "medium" ? "?size=medium" : ""}`;
}

function isActive(job) {
  return job.status === "pending" || job.status === "generating";
}

function shortPrompt(prompt, length = 80) {
  const text = prompt.replace(/\s+/g, " ").trim();
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function svgIcon(id) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

function badge(status) {
  return el("span", { className: `badge ${status}` }, status);
}

function prefersMotion() {
  return !matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// --- the model and its options ---

function currentModel() {
  return state.models.find((model) => model.name === $("model").value) ?? null;
}

async function loadModels() {
  try {
    const { models, default_model: defaultModel } = await api("/api/models");
    state.models = models;
    $("model").replaceChildren(
      ...models.map((model) => new Option(model.default ? `${model.name} (default)` : model.name, model.name)),
    );
    $("model").value = defaultModel ?? models[0]?.name ?? "";
    $("model").disabled = models.length === 0;
    if (models.length === 0) showMessage("No models are configured on this server: ask its administrator.", "error");
  } catch (error) {
    showMessage(`The models could not be loaded: ${error.message}`, "error");
  }
  renderOptions();
}

// The limits the form checks before sending (the server checks them again).
async function loadLimits() {
  try {
    const { server } = await api("/api/info");
    state.maxUploadMb = server.max_upload_mb;
    $("prompt").maxLength = server.max_prompt_chars;
    updateDropzone();
  } catch {
    // keep the defaults: the server refuses what exceeds its limits anyway
  }
}

// Only the chosen model's options, each at "Default" (the model's, or else the provider's)
// unless chosen; a value chosen for another model with the same option is kept.
function renderOptions() {
  const model = currentModel();
  for (const field of $("option-fields").querySelectorAll("[data-option]")) field.remove();
  $("option-fields").append(...(model?.options ?? []).map(optionField));
  $("model-hint").textContent = model ? model.description || model.upstream_model : "";
  renderReferences();
}

function optionLabel(name) {
  return OPTION_LABELS[name] ?? name.charAt(0).toUpperCase() + name.slice(1).replaceAll("_", " ");
}

function optionField(option) {
  const control = option.type === "enum" ? enumControl(option) : inputControl(option);
  control.id = `option-${option.name}`;
  control.name = option.name;
  control.title = option.description;
  const remembered = state.optionValues[option.name];
  if (remembered !== undefined && acceptsValue(option, remembered)) control.value = remembered;
  control.addEventListener("input", () => {
    state.optionValues[option.name] = control.value;
  });
  const label = el("label", { for: control.id }, optionLabel(option.name));
  return el("div", { className: "field", "data-option": option.name }, label, control);
}

function enumControl(option) {
  const select = el("select");
  const label = option.default === null ? "Default" : `Default (${option.default})`;
  select.append(new Option(label, ""), ...option.values.map((value) => new Option(value, value)));
  return select;
}

function inputControl(option) {
  const integer = option.type === "integer";
  const input = el("input", { type: integer ? "number" : "text", autocomplete: "off", spellcheck: "false" });
  if (integer) {
    Object.assign(input, { min: option.minimum, max: option.maximum, step: 1, inputMode: "numeric" });
  } else if (patternWorks(option.pattern)) {
    input.pattern = option.pattern; // the browser then flags a malformed value before it is sent
  }
  if (option.default !== null) input.placeholder = `Default (${option.default})`;
  else input.placeholder = OPTION_EXAMPLES[option.name] ?? "Default";
  return input;
}

// The server's patterns are Python's; the browser compiles a pattern attribute with the v flag.
function patternWorks(pattern) {
  if (!pattern) return false;
  try {
    new RegExp(`^(?:${pattern})$`, "v");
    return true;
  } catch {
    return false;
  }
}

function acceptsValue(option, value) {
  if (value === "") return true;
  if (option.type === "enum") return option.values.includes(value);
  if (option.type === "integer") return /^\d+$/.test(value) && option.minimum <= +value && +value <= option.maximum;
  return !patternWorks(option.pattern) || new RegExp(`^(?:${option.pattern})$`, "v").test(value);
}

function chosenOptions() {
  const options = {};
  for (const control of $("option-fields").querySelectorAll("[data-option] select, [data-option] input")) {
    const value = control.value.trim();
    if (value) options[control.name] = value; // left at Default: not sent
  }
  return options;
}

// The form as `job` was asked for. Its options include the model's defaults: those stay "Default".
function fillForm(job) {
  $("prompt").value = job.prompt;
  const model = state.models.find((item) => item.name === job.model);
  if (!model) {
    updateGenerateButton();
    return `The prompt is in the form, but the model ${job.model} is no longer available: choose another one.`;
  }
  $("model").value = model.name;
  state.optionValues = {};
  for (const option of model.options) {
    const value = job.options[option.name];
    const isDefault = value === undefined || String(value) === String(option.default);
    state.optionValues[option.name] = isDefault ? "" : String(value);
  }
  renderOptions();
  return null;
}

// --- reference images ---

function imageType(file) {
  if (file.type) return file.type in IMAGE_TYPES ? file.type : null;
  return EXTENSION_TYPES[file.name.split(".").pop().toLowerCase()] ?? null;
}

// Adds the acceptable files, as many as the model takes; returns what went wrong, if anything.
function addFiles(files) {
  const model = currentModel();
  const limit = model?.max_reference_images ?? 0;
  const problems = [];
  let skipped = 0;
  for (const file of files) {
    const name = file.name || "The pasted image";
    if (!imageType(file)) {
      problems.push(`${name}: not a PNG, JPEG, WebP, GIF or SVG image.`);
    } else if (file.size === 0) {
      problems.push(`${name}: the file is empty.`);
    } else if (file.size > state.maxUploadMb * 1024 ** 2) {
      problems.push(`${name}: ${formatSize(file.size)}, more than the ${state.maxUploadMb} MB a reference image may have.`);
    } else if (state.references.length >= limit) {
      skipped += 1;
    } else {
      state.references.push({ file, url: URL.createObjectURL(file) });
    }
  }
  if (skipped) {
    const modelName = model?.name ?? "The chosen model";
    problems.push(
      limit === 0
        ? `${modelName} takes no reference images.`
        : `${modelName} takes at most ${plural(limit, "reference image")}: ` +
            `${plural(skipped, "image was", "images were")} not added.`,
    );
  }
  renderReferences();
  return problems;
}

// From the dropzone, a drop or a paste: the outcome goes to the form's message line.
function addChosenFiles(files) {
  const before = state.references.length;
  const problems = addFiles(files);
  const added = state.references.length - before;
  if (problems.length) showMessage(problems.join("\n"), "error");
  else if (added) showMessage(`${plural(added, "reference image")} added.`, "ok");
}

function removeReference(reference) {
  const index = state.references.indexOf(reference);
  URL.revokeObjectURL(reference.url); // only this page's copy: nothing stored is touched
  state.references.splice(index, 1);
  renderReferences();
  // The focus moves to the next Remove button (or the dropzone), not to the top of the page.
  const buttons = $("reference-list").querySelectorAll("button");
  (buttons[Math.min(index, buttons.length - 1)] ?? $("dropzone")).focus();
}

function renderReferences() {
  const limit = currentModel()?.max_reference_images ?? 0;
  $("reference-list").replaceChildren(
    ...state.references.map((reference, index) => referenceItem(reference, index >= limit)),
  );
  $("reference-list").hidden = state.references.length === 0;
  updateDropzone();
  updateGenerateButton();
}

function referenceItem(reference, overLimit) {
  const { file, url } = reference;
  const remove = el("button", { type: "button", className: "secondary small", "aria-label": `Remove ${file.name}` }, "Remove");
  remove.addEventListener("click", () => removeReference(reference));
  return el(
    "li",
    { className: overLimit ? "reference over-limit" : "reference" },
    el("img", { src: url, alt: file.name, className: "checkered" }),
    el("span", { className: "name", title: `${file.name} (${formatSize(file.size)})` }, file.name),
    remove,
  );
}

// What the dropzone says (and whether it takes files), from the model's limit and the files chosen.
function updateDropzone() {
  const model = currentModel();
  const limit = model?.max_reference_images ?? 0;
  const count = state.references.length;
  const disabled = !model || count >= limit;
  let title;
  let hint;
  if (count > limit) {
    title = "Too many reference images";
    hint = limit === 0
      ? `${model?.name ?? "This model"} takes none: remove them or choose another model`
      : `${model.name} takes at most ${limit}: remove ${count - limit} or choose another model`;
  } else if (!model || limit === 0) {
    title = "No reference images";
    hint = model ? `${model.name} works from the prompt alone` : "Choose a model first";
  } else if (count === 0) {
    const needed = model.min_reference_images;
    title = needed ? `Drop at least ${needed} image${needed === 1 ? "" : "s"} here` : "Drop images here";
    hint = `${needed ? `${model.name} edits images: it needs ${needed} or more\n` : "or click to choose, or paste one\n"}` +
      `PNG, JPEG, WebP, GIF or SVG · up to ${limit}, ${state.maxUploadMb} MB each`;
  } else if (count < model.min_reference_images) {
    title = `${count} of at least ${model.min_reference_images} chosen`;
    hint = `${model.name} needs ${model.min_reference_images - count} more`;
  } else {
    title = `${count} of ${limit} chosen`;
    hint = count < limit ? "Click, drop or paste to add more" : `The most ${model.name} takes`;
  }
  const zone = $("dropzone");
  zone.classList.toggle("disabled", disabled);
  zone.classList.toggle("over", count > limit);
  zone.setAttribute("aria-disabled", String(disabled));
  zone.tabIndex = disabled ? -1 : 0;
  $("reference-input").disabled = disabled;
  $("drop-title").textContent = title;
  $("drop-hint").textContent = hint;
}

function setUpDropzone() {
  const zone = $("dropzone");
  zone.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && !$("reference-input").disabled) {
      event.preventDefault();
      $("reference-input").click();
    }
  });
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      if (!$("reference-input").disabled) zone.classList.add("dragging");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, () => zone.classList.remove("dragging"));
  }
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    addChosenFiles([...event.dataTransfer.files]);
  });
  $("reference-input").addEventListener("change", () => {
    addChosenFiles([...$("reference-input").files]);
    $("reference-input").value = ""; // so that choosing the same file again is noticed
  });
  // A file dropped beside the dropzone would make the browser leave the page to show it.
  for (const type of ["dragover", "drop"]) {
    document.addEventListener(type, (event) => {
      if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
    });
  }
  // A pasted image becomes a reference image, unless it comes with text pasted into a text field.
  document.addEventListener("paste", (event) => {
    if ($("job-dialog").open) return;
    const files = [...(event.clipboardData?.files ?? [])];
    if (files.length === 0) return;
    const editable = event.target.closest?.("input, textarea, [contenteditable]");
    if (editable && event.clipboardData.types.includes("text/plain")) return;
    event.preventDefault();
    addChosenFiles(files);
  });
}

// --- generating ---

function updateGenerateButton() {
  const model = currentModel();
  $("generate-button").disabled =
    state.submitting ||
    !model ||
    !$("prompt").value.trim() ||
    state.references.length > model.max_reference_images ||
    state.references.length < model.min_reference_images;
}

async function generate() {
  if ($("generate-button").disabled) return;
  if (!$("generate-form").checkValidity()) {
    $("generate-form").reportValidity(); // an option out of range or malformed
    return;
  }
  const body = new FormData();
  body.append("prompt", $("prompt").value);
  body.append("model", currentModel().name);
  for (const [name, value] of Object.entries(chosenOptions())) body.append(name, value);
  for (const { file } of state.references) body.append("reference", file, file.name);
  state.submitting = true;
  updateGenerateButton();
  const count = state.references.length;
  showMessage(count ? `Uploading ${plural(count, "reference image")}…` : "Sending…");
  try {
    const job = await api("/api/jobs", { method: "POST", body });
    // Everything stays in the form, to change a detail and generate again.
    showMessage(`Generating with ${job.model}: the image appears in the history below.`, "ok");
    state.page = 1; // where the new job is listed
    await loadJobs();
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    state.submitting = false;
    updateGenerateButton();
  }
}

function setUpForm() {
  $("generate-form").addEventListener("submit", (event) => {
    event.preventDefault();
    generate();
  });
  $("prompt").addEventListener("input", updateGenerateButton);
  $("prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      generate();
    }
  });
  $("model").addEventListener("change", renderOptions);
  const platform = navigator.userAgentData?.platform ?? navigator.platform;
  if (/mac|iphone|ipad/i.test(platform)) $("shortcut").textContent = "or ⌘+Enter in the prompt";
}

// --- the history ---

async function loadJobs() {
  clearTimeout(state.refreshTimer);
  const load = ++state.loads;
  let active = false;
  try {
    const list = await api(`/api/jobs?page=${state.page}&per_page=${PAGE_SIZE}`);
    if (load !== state.loads) return; // a newer load (of another page, say) shows its own result
    const pages = Math.max(1, Math.ceil(list.total / PAGE_SIZE));
    if (state.page > pages) {
      state.page = pages; // past the last page: show the last one
      return loadJobs();
    }
    const rows = JSON.stringify(list.jobs);
    if (rows !== state.rows) {
      // Only when it changed: the list is reloaded every few seconds, and an open error stays open.
      state.rows = rows;
      state.jobs = list.jobs;
      renderJobs(list.jobs);
    }
    $("job-grid").hidden = list.total === 0;
    $("no-jobs").hidden = list.total > 0;
    $("pager").hidden = pages === 1;
    $("page-info").textContent = `Page ${state.page} of ${pages} · ${plural(list.total, "job")}`;
    $("previous-page").disabled = state.page === 1;
    $("next-page").disabled = state.page === pages;
    active = list.jobs.some(isActive);
    if (await refreshDialog(list.jobs)) active = true;
  } catch (error) {
    if (load !== state.loads) return;
    showMessage(error.message, "error");
  }
  state.refreshTimer = setTimeout(loadJobs, active ? 2000 : 30000);
}

function goToPage(page) {
  state.page = Math.max(1, page);
  loadJobs();
}

function renderJobs(jobs) {
  hideError();
  // A card that had the focus keeps it across the re-rendering.
  const focused = document.activeElement?.closest?.(".job-card")?.dataset.job;
  $("job-grid").replaceChildren(...jobs.map(jobCard));
  if (focused) $("job-grid").querySelector(`[data-job="${focused}"] .job-open`)?.focus();
}

function jobCard(job) {
  const counts = [];
  if (job.images.length > 1) counts.push(plural(job.images.length, "image"));
  if (job.reference_count) counts.push(plural(job.reference_count, "reference image"));
  const label = [`Open job, ${job.status}: ${shortPrompt(job.prompt)}`, ...counts].join(", ");
  const open = el("button", { type: "button", className: "job-open", "aria-label": label }, jobThumb(job));
  open.addEventListener("click", () => openJob(job, open));

  const status = el("div", { className: "job-status" }, badge(job.status));
  if (job.error) status.append(errorHint(job.error));
  const cost = job.usage?.cost_usd;
  const meta = el(
    "p",
    { className: "job-meta" },
    el("span", { className: "model" }, job.model),
    cost === null || cost === undefined ? null : el("span", { className: "cost", title: "Cost reported by the provider" }, formatCost(cost)),
    el("time", { datetime: job.created_at }, formatTime(job.created_at)),
  );
  return el(
    "li",
    { className: "job-card", "data-job": job.id },
    open,
    el("div", { className: "job-body" }, status, el("p", { className: "job-prompt" }, job.prompt), meta),
  );
}

function jobThumb(job) {
  const thumb = el("div", { className: "job-thumb" });
  const [first] = job.images;
  if (first) {
    const img = el("img", { src: imageUrl(job.id, first.index, "medium"), alt: "", loading: "lazy", decoding: "async" });
    img.addEventListener("error", () => img.replaceWith(placeholder("completed", "Preview unavailable")), { once: true });
    thumb.append(img);
  } else {
    thumb.append(placeholder(job.status));
  }
  const markers = el("div", { className: "markers" });
  if (job.images.length > 1) markers.append(marker("icon-images", job.images.length, plural(job.images.length, "image")));
  if (job.reference_count) {
    markers.append(marker("icon-paperclip", job.reference_count, plural(job.reference_count, "reference image")));
  }
  if (markers.childElementCount) thumb.append(markers);
  return thumb;
}

function marker(icon, count, title) {
  return el("span", { className: "marker", title }, svgIcon(icon), String(count));
}

// What a job without a picture shows instead: its status (with more to say in the dialog).
function placeholder(status, text = PLACEHOLDERS[status]?.[1]) {
  const [icon] = PLACEHOLDERS[status] ?? PLACEHOLDERS.completed;
  return el("div", { className: `placeholder ${status}` }, svgIcon(icon), el("span", {}, text ?? "No image"));
}

// --- the error of a failed job, over the page next to its "?" (as TeXdrop shows it) ---

// A failed job's "?": its error is shown over the page while the pointer is on it (or it
// has the focus), so that long errors do not make the cards taller.
function errorHint(error) {
  const hint = document.createElement("button");
  hint.type = "button";
  hint.className = "error-hint";
  hint.textContent = "?";
  hint.setAttribute("aria-label", `Error: ${error}`);
  hint.addEventListener("mouseenter", () => showError(hint, error));
  hint.addEventListener("focus", () => showError(hint, error));
  hint.addEventListener("mouseleave", hideErrorSoon);
  hint.addEventListener("blur", hideErrorSoon);
  return hint;
}

function showError(hint, error) {
  clearTimeout(shownError.hideTimer);
  shownError.hint = hint;
  $("error-tooltip").textContent = error;
  $("error-tooltip").hidden = false;
  placeError();
}

// Below its "?", or above it if there is no room, and always inside the window.
function placeError() {
  const tooltip = $("error-tooltip");
  tooltip.style.left = tooltip.style.top = "0"; // measured where it has the whole width
  const anchor = shownError.hint.getBoundingClientRect();
  const { clientWidth, clientHeight } = document.documentElement;
  const below = anchor.bottom + 6;
  const top = below + tooltip.offsetHeight <= clientHeight - 8 ? below : anchor.top - 6 - tooltip.offsetHeight;
  tooltip.style.top = `${Math.max(8, top)}px`;
  tooltip.style.left = `${Math.max(8, Math.min(anchor.left, clientWidth - 8 - tooltip.offsetWidth))}px`;
}

function hideError() {
  clearTimeout(shownError.hideTimer);
  $("error-tooltip").hidden = true;
}

// After a moment, to let the pointer move onto the error (to select or scroll it); not while the
// pointer is on the error or its "?" (clicking into the error takes the focus from the "?").
function hideErrorSoon() {
  shownError.hideTimer = setTimeout(() => {
    if (!$("error-tooltip").matches(":hover") && !shownError.hint.matches(":hover")) hideError();
  }, 200);
}

// --- the job dialog ---

function openJob(job, opener = null) {
  hideError();
  state.opener = opener;
  renderDialog(job);
  showMessage("", "", $("dialog-message"));
  if (!$("job-dialog").open) $("job-dialog").showModal();
  history.replaceState(null, "", `?job=${job.id}`); // a link to the page with this job open
  if (isActive(job)) loadJobs(); // followed every 2 s, even when it is not on the page shown
}

// `focus`: what gets the focus once the dialog is closed (by default, the card that opened it).
function closeDialog(focus = null) {
  state.focusAfterClose = focus;
  $("job-dialog").close();
}

function onDialogClosed() {
  const id = state.dialogJob?.id;
  state.dialogJob = state.dialogJson = null;
  history.replaceState(null, "", location.pathname);
  const card = $("job-grid").querySelector(`[data-job="${id}"] .job-open`);
  const chosen = state.focusAfterClose;
  const target = chosen ?? (state.opener?.isConnected ? state.opener : card);
  state.focusAfterClose = state.opener = null;
  target?.focus({ preventScroll: Boolean(chosen) }); // who chose it scrolls to it
}

// The open dialog's job, if still in progress, is followed: from the page's list, or on its own.
// Returns whether it is still in progress.
async function refreshDialog(jobs) {
  const shown = state.dialogJob;
  if (!$("job-dialog").open || !shown || !isActive(shown)) return false;
  const job = jobs.find((item) => item.id === shown.id) ?? (await api(`/api/jobs/${shown.id}`));
  if (state.dialogJob?.id === job.id && JSON.stringify(job) !== state.dialogJson) renderDialog(job);
  return isActive(job);
}

function renderDialog(job) {
  const dialog = $("job-dialog");
  const hadFocus = dialog.contains(document.activeElement);
  state.dialogJob = job;
  state.dialogJson = JSON.stringify(job);
  $("dialog-title").textContent = `Job ${job.id}`;
  $("dialog-status").replaceChildren(badge(job.status));
  $("dialog-images").replaceChildren(...dialogImages(job));
  $("dialog-images").classList.toggle("several", job.images.length > 1);
  $("dialog-prompt").textContent = job.prompt;
  $("dialog-reply").hidden = !job.text;
  $("dialog-text").textContent = job.text ?? "";
  $("dialog-facts").replaceChildren(...jobFacts(job));
  if (dialog.open && hadFocus && !dialog.contains(document.activeElement)) $("dialog-close").focus();
}

function dialogImages(job) {
  if (job.images.length === 0) {
    const waiting = job.reference_count - job.references.length;
    const text = {
      pending: `Waiting for ${plural(waiting, "reference image")} to be uploaded before generating`,
      generating: `Generating with ${job.model}…`,
      failed: "No image: the generation failed (the error is below)",
    }[job.status];
    return [el("div", { className: "dialog-placeholder" }, placeholder(job.status, text))];
  }
  return job.images.map((image) => preview(job, image));
}

function preview(job, image) {
  const full = imageUrl(job.id, image.index);
  const medium = imageUrl(job.id, image.index, "medium");
  const several = job.images.length > 1;
  const alt = `${several ? `Image ${image.index}` : "Image"} generated from: ${shortPrompt(job.prompt, 150)}`;
  const use = el("button", { type: "button", className: "secondary small" }, "Use as reference");
  use.addEventListener("click", () => useAsReference(job, image, use));
  const caption = [
    several ? `Image ${image.index}` : null,
    `${image.width} × ${image.height}`,
    IMAGE_TYPES[image.content_type] ?? image.content_type,
    formatSize(image.bytes),
  ];
  return el(
    "figure",
    { className: "preview" },
    el(
      "a",
      { className: "preview-link checkered", href: full, target: "_blank", rel: "noopener", title: "Open the full size in a new tab" },
      el("img", { src: medium, alt, width: image.width, height: image.height }),
    ),
    el("figcaption", {}, caption.filter(Boolean).join(" · ")),
    el(
      "div",
      { className: "actions" },
      el("a", { className: "pill small", href: full, download: image.filename }, "Download"),
      el("a", { className: "pill small", href: full, target: "_blank", rel: "noopener" }, "Open full size"),
      el("a", { className: "pill small", href: medium, target: "_blank", rel: "noopener" }, "Open medium"),
      use,
    ),
  );
}

function jobFacts(job) {
  const rows = [
    ["Status", statusText(job)],
    ["Job id", el("code", {}, job.id)],
    ["Model", modelValue(job)],
    ["Options", optionsText(job.options)],
    ["Reference images", referencesValue(job)],
    ["Created", formatTime(job.created_at)],
    ["Started", formatTime(job.started_at) || "–"],
    ["Finished", formatTime(job.finished_at) || "–"],
    ["Duration", formatDuration(job.duration_seconds)],
    ["Tokens", tokensText(job.usage)],
    ["Cost", job.usage?.cost_usd === null || job.usage?.cost_usd === undefined ? "Not reported" : formatCost(job.usage.cost_usd)],
    ["Source", SOURCES[job.source] ?? job.source],
    ["Client IP", el("code", {}, job.client_ip)],
  ];
  if (job.error) rows.push(["Error", el("span", { className: "dialog-error" }, job.error)]);
  return rows.flatMap(([term, value]) => [el("dt", {}, term), el("dd", {}, value)]);
}

function statusText(job) {
  if (job.status === "pending") {
    return `pending: ${job.references.length} of ${plural(job.reference_count, "reference image")} uploaded`;
  }
  if (job.status === "completed") return `completed: ${plural(job.images.length, "image")}`;
  return job.status;
}

function modelValue(job) {
  const details = `${job.upstream_model} · ${job.api_type} API · ${job.provider}`;
  return el("span", {}, job.model, el("br"), el("span", { className: "muted" }, details));
}

function optionsText(options) {
  const entries = Object.entries(options);
  if (entries.length === 0) return "None: the provider's defaults";
  return entries.map(([name, value]) => `${optionLabel(name)} ${value}`).join(" · ");
}

function referencesValue(job) {
  if (job.reference_count === 0) return "None";
  const thumbs = job.references.map((reference) =>
    el(
      "a",
      {
        href: referenceUrl(job.id, reference.index),
        target: "_blank",
        rel: "noopener",
        title: `${reference.original_name ?? reference.filename} · ${reference.width} × ${reference.height}`,
        className: "checkered",
      },
      el("img", {
        src: referenceUrl(job.id, reference.index, "medium"),
        alt: `Reference image ${reference.index}: ${reference.original_name ?? reference.filename}`,
        loading: "lazy",
      }),
    ),
  );
  const missing = job.reference_count - job.references.length;
  return el(
    "div",
    {},
    thumbs.length ? el("div", { className: "ref-thumbs" }, ...thumbs) : null,
    missing ? el("span", { className: "muted" }, `${plural(missing, "image")} not uploaded`) : null,
  );
}

function tokensText(usage) {
  if (!usage || usage.total_tokens === null) return "Not reported";
  const parts = [];
  if (usage.prompt_tokens !== null) parts.push(`${formatNumber(usage.prompt_tokens)} prompt`);
  if (usage.completion_tokens !== null) parts.push(`${formatNumber(usage.completion_tokens)} completion`);
  const total = `${formatNumber(usage.total_tokens)} total`;
  return parts.length ? `${total} (${parts.join(", ")})` : total;
}

function scrollToForm() {
  $("generate").scrollIntoView({ block: "start", behavior: prefersMotion() ? "smooth" : "auto" });
}

// The full-size image, as a file among the form's reference images.
async function useAsReference(job, image, button) {
  const model = currentModel();
  const limit = model?.max_reference_images ?? 0;
  if (state.references.length >= limit) {
    const text = limit === 0
      ? `${model?.name ?? "The chosen model"} takes no reference images: choose another model in the form first.`
      : `${model.name} takes at most ${plural(limit, "reference image")}, and ${limit} are chosen already.`;
    showMessage(text, "error", $("dialog-message"));
    return;
  }
  button.disabled = true;
  try {
    const blob = await (await request(imageUrl(job.id, image.index))).blob();
    const problems = addFiles([new File([blob], image.filename, { type: blob.type || image.content_type })]);
    if (problems.length) {
      showMessage(problems.join("\n"), "error", $("dialog-message"));
      return;
    }
    closeDialog($("prompt"));
    showMessage(`${image.filename} is now a reference image: describe what to make of it.`, "ok");
    scrollToForm();
  } catch (error) {
    showMessage(error.message, "error", $("dialog-message"));
  } finally {
    button.disabled = false;
  }
}

function reusePrompt() {
  const job = state.dialogJob;
  const problem = fillForm(job);
  closeDialog($("prompt"));
  showMessage(problem ?? "The prompt, model and options are in the form: change what you like and generate.", problem ? "error" : "ok");
  scrollToForm();
}

async function copyPrompt() {
  const button = $("copy-prompt");
  try {
    await copyText(state.dialogJob.prompt);
    button.textContent = "Copied";
    $("dialog-live").textContent = "Prompt copied.";
    setTimeout(() => {
      button.textContent = "Copy prompt";
      $("dialog-live").textContent = "";
    }, 1500);
  } catch (error) {
    showMessage(`Copying failed: ${error.message}`, "error", $("dialog-message"));
  }
}

// Opens the job named in the address (?job=<id>), from the page shown or from the server.
async function openFromAddress() {
  const id = new URLSearchParams(location.search).get("job");
  if (!id) return;
  try {
    if (!JOB_ID.test(id)) throw new Error("no such job");
    openJob(state.jobs.find((job) => job.id === id) ?? (await api(`/api/jobs/${id}`)));
  } catch (error) {
    history.replaceState(null, "", location.pathname);
    showMessage(`Cannot open job ${id}: ${error.message}`, "error");
  }
}

function setUpDialog() {
  const dialog = $("job-dialog");
  dialog.addEventListener("close", onDialogClosed);
  $("dialog-close").addEventListener("click", () => closeDialog());
  // A click on the backdrop closes the dialog; not a text selection that ends there.
  dialog.addEventListener("pointerdown", (event) => {
    state.pointerDownOnBackdrop = event.target === dialog;
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog && state.pointerDownOnBackdrop) closeDialog();
  });
  $("copy-prompt").addEventListener("click", copyPrompt);
  $("reuse-prompt").addEventListener("click", reusePrompt);
}

setUpForm();
setUpDropzone();
setUpDialog();
$("refresh").addEventListener("click", loadJobs);
$("previous-page").addEventListener("click", () => goToPage(state.page - 1));
$("next-page").addEventListener("click", () => goToPage(state.page + 1));
$("error-tooltip").addEventListener("mouseenter", () => clearTimeout(shownError.hideTimer));
$("error-tooltip").addEventListener("mouseleave", hideErrorSoon);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideError();
});
// An open error follows its "?" when the page scrolls (not when the error itself does).
addEventListener(
  "scroll",
  (event) => {
    if (!$("error-tooltip").hidden && event.target !== $("error-tooltip")) placeError();
  },
  { capture: true, passive: true },
);
loadModels();
loadLimits();
loadJobs().then(openFromAddress);
