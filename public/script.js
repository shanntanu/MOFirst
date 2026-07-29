// Defaults to the same origin the page was loaded from - the form (public/)
// and the API (api/) are both served from the same Vercel deployment.
// Override window.MO_API_BASE before this script runs only for local testing
// against `python api/index.py` directly (see the README).
const API_BASE = window.MO_API_BASE || window.location.origin;

const PHONE_RE = /^[6-9]\d{9}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function initRegistrationForm() {
  const form = document.getElementById("regForm");
  const fullNameEl = document.getElementById("fullName");
  const contactEl = document.getElementById("contactNumber");
  const whatsappEl = document.getElementById("whatsappNumber");
  const emailEl = document.getElementById("emailId");
  const agreeEl = document.getElementById("agreeConnect");
  const proceedBtn = document.getElementById("proceedBtn");
  const proceedLabel = document.getElementById("proceedLabel");
  const statusEl = document.getElementById("formStatus");

  if (!form || !proceedBtn) return;

  function setFieldError(fieldId, hasError) {
    const field = document.getElementById(fieldId);
    if (field) field.classList.toggle("has-error", hasError);
  }

  function validate(showErrors) {
    let valid = true;

    const nameOk = fullNameEl.value.trim().length > 0;
    if (!nameOk) valid = false;
    if (showErrors) setFieldError("field-name", !nameOk);

    const contactOk = PHONE_RE.test(contactEl.value.trim());
    if (!contactOk) valid = false;
    if (showErrors) setFieldError("field-contact", !contactOk);

    const whatsappVal = whatsappEl.value.trim();
    const whatsappOk = whatsappVal === "" || PHONE_RE.test(whatsappVal);
    if (!whatsappOk) valid = false;
    if (showErrors) setFieldError("field-whatsapp", !whatsappOk);

    const emailVal = emailEl.value.trim();
    const emailOk = emailVal === "" || EMAIL_RE.test(emailVal);
    if (!emailOk) valid = false;
    if (showErrors) setFieldError("field-email", !emailOk);

    if (!agreeEl.checked) valid = false;

    return valid;
  }

  function refreshButtonState() {
    const enabled = validate(false);
    proceedBtn.disabled = !enabled;
    proceedBtn.classList.toggle("enabled", enabled);
  }

  // Delegate on the form so every keystroke, paste, and checkbox toggle
  // is caught with a single pair of listeners.
  form.addEventListener("input", refreshButtonState);
  form.addEventListener("change", refreshButtonState);
  form.addEventListener("click", (e) => {
    if (e.target === agreeEl) refreshButtonState();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (!validate(true)) {
      refreshButtonState();
      return;
    }

    proceedBtn.disabled = true;
    proceedLabel.textContent = "Submitting...";
    statusEl.textContent = "";
    statusEl.className = "form-status";

    const payload = {
      full_name: fullNameEl.value.trim(),
      contact_number: contactEl.value.trim(),
      whatsapp_number: whatsappEl.value.trim() || null,
      email: emailEl.value.trim() || null,
      agree_connect: agreeEl.checked,
    };

    try {
      const res = await fetch(`${API_BASE}/api/register`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // Skips ngrok's free-tier browser warning interstitial, which
          // otherwise returns an HTML page instead of JSON for new visitors.
          "ngrok-skip-browser-warning": "true",
        },
        body: JSON.stringify(payload),
      });

      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        throw new Error(data.error || "Submission failed. Please try again.");
      }

      document.getElementById("screen-form").classList.add("hidden");
      document.getElementById("screen-thankyou").classList.remove("hidden");
    } catch (err) {
      statusEl.textContent = err.message || "Something went wrong. Please try again.";
      statusEl.className = "form-status error";
      proceedLabel.textContent = "Proceed";
      refreshButtonState();
    }
  });

  // Initial state in case the browser restores field values on reload.
  refreshButtonState();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initRegistrationForm);
} else {
  initRegistrationForm();
}
