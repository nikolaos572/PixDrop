"use strict";

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("login-error");
  error.textContent = "";
  try {
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({ token: document.getElementById("token").value.trim() }),
    });
    if (response.ok) {
      location.reload(); // the server now answers with the page asked for
    } else if (response.status === 401 || response.status === 422) {
      error.textContent = "Invalid token."; // 422: longer than any token can be, so not a valid one either
    } else {
      error.textContent = `Login failed: ${await reason(response)}`;
    }
  } catch {
    error.textContent = "The server cannot be reached.";
  }
});

// The server's reason ({"detail": ...}), such as a page opened at another address, or the status.
async function reason(response) {
  try {
    const { detail } = await response.json();
    if (typeof detail === "string") return detail;
  } catch {
    // no JSON body: the status will do
  }
  return `${response.status} ${response.statusText}`;
}
