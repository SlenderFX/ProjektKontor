document.querySelectorAll('.login-url').forEach(element => {
  element.textContent = `${window.location.origin}/login`;
});
document.querySelector('#print')?.addEventListener('click', () => window.print());
