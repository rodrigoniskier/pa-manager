document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      const button = form.querySelector("button[type='submit']");
      if (button && !button.disabled) {
        button.dataset.originalText = button.textContent;
        button.textContent = "Processando…";
        button.disabled = true;
      }
    });
  });

  const runStatus = document.body.dataset.runStatus;
  if (runStatus === "queued" || runStatus === "running") {
    window.setTimeout(() => window.location.reload(), 2000);
  }
});
