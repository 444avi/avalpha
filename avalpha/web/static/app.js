// Confirm dialogs for destructive / token-spending actions, and a "working…"
// state on the button that was clicked so double-submits are obvious.
document.addEventListener("submit", function (e) {
  const form = e.target;
  const btn = form.querySelector("[data-confirm]");
  if (btn && !window.confirm(btn.getAttribute("data-confirm"))) {
    e.preventDefault();
    return;
  }
  const submitter = e.submitter || form.querySelector("button[type=submit], button:not([type])");
  if (submitter && !submitter.classList.contains("mini")) {
    submitter.dataset.label = submitter.textContent;
    submitter.textContent = "Working…";
    submitter.disabled = true;
    // Re-enable on bfcache restore (back button) so the page isn't stuck.
    setTimeout(() => { submitter.disabled = false; }, 8000);
  }
});

// Buttonless toggles (e.g. the swing-alert opt-in) post their form on change.
document.addEventListener("change", function (e) {
  const el = e.target;
  if (el.matches("[data-autosubmit]") && el.form) {
    el.form.submit();
  }
});
