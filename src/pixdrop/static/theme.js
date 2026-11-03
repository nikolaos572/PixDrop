"use strict";

// The colour theme: Light, Dark, or Auto (the browser's), remembered in this browser.
// Loaded in <head> without defer, so that pages are painted in the right theme from the start.

const THEME_KEY = "theme";
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
let themeChoice = readThemeChoice();

function readThemeChoice() {
  try {
    const choice = localStorage.getItem(THEME_KEY);
    return choice === "light" || choice === "dark" ? choice : "auto";
  } catch {
    return "auto"; // storage blocked by the browser
  }
}

function applyTheme() {
  const dark = themeChoice === "dark" || (themeChoice === "auto" && darkQuery.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  for (const button of document.querySelectorAll("[data-theme-choice]")) {
    button.setAttribute("aria-pressed", String(button.dataset.themeChoice === themeChoice));
  }
}

function chooseTheme(choice) {
  themeChoice = choice;
  try {
    if (choice === "auto") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, choice);
  } catch {
    // storage blocked: the choice lasts until the page is left
  }
  applyTheme();
}

applyTheme();
darkQuery.addEventListener("change", applyTheme); // Auto follows the browser as it changes
document.addEventListener("DOMContentLoaded", () => {
  for (const button of document.querySelectorAll("[data-theme-choice]")) {
    button.addEventListener("click", () => chooseTheme(button.dataset.themeChoice));
  }
  applyTheme(); // marks the chosen button
});
