// Login screen: sends the credentials in the request body and opens the app on success.
const form = document.querySelector("#login");
const error = document.querySelector("#error");
const submit = document.querySelector("#submit");

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  submit.disabled = true;
  error.classList.add("hidden");
  const body = JSON.stringify({ username: form.username.value.trim(), password: form.password.value.trim() });
  try {
    const r = await fetch("/api/session", { method: "POST", headers: { "content-type": "application/json" }, credentials: "same-origin", body });
    if (r.ok) { location.href = "/"; return; }
    const d = await r.json().catch(() => ({}));
    error.textContent = d.detail || `Chyba ${r.status}`;
  } catch {
    error.textContent = "Server neodpovídá.";
  }
  error.classList.remove("hidden");
  submit.disabled = false;
});
