document.querySelectorAll('input[name="phone"][pattern="[0-9]{10}"]').forEach((input) => {
  input.addEventListener("input", () => {
    const start = input.selectionStart;
    const beforeCursor = input.value.slice(0, start).replace(/[^0-9]/g, "").slice(0, 10);
    input.value = input.value.replace(/[^0-9]/g, "").slice(0, 10);
    input.setSelectionRange(beforeCursor.length, beforeCursor.length);
  });
});

document.querySelectorAll("[data-password-toggle]").forEach((button) => {
  const input = document.getElementById(button.getAttribute("aria-controls"));
  if (!input || input.type !== "password") return;
  button.hidden = false;
  button.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? button.dataset.hide : button.dataset.show;
    button.setAttribute("aria-pressed", String(show));
  });
});
