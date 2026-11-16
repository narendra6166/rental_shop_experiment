document.querySelectorAll('input[name="phone"][pattern="[0-9]{10}"]').forEach((input) => {
  input.addEventListener("input", () => {
    const start = input.selectionStart;
    const beforeCursor = input.value.slice(0, start).replace(/[^0-9]/g, "").slice(0, 10);
    input.value = input.value.replace(/[^0-9]/g, "").slice(0, 10);
    input.setSelectionRange(beforeCursor.length, beforeCursor.length);
  });
});
