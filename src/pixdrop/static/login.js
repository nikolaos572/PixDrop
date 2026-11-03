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
    } else {
      // 422: longer than any token can be, so not a valid one either.
      const invalid = response.status === 401 || response.status === 422;
      error.textContent = invalid ? "Invalid token." : `Login failed (${response.status}).`;
    }
  } catch {
    error.textContent = "The server cannot be reached.";
  }
});
