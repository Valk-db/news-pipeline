// htmx ignores 4xx/5xx responses by default, which makes failures look like "nothing happened".
document.addEventListener('htmx:responseError', (e) => {
  const text = (e.detail.xhr.responseText || '')
    .replace(/<style[\s\S]*?<\/style>/g, ' ')
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 300);
  alert('Request failed (' + e.detail.xhr.status + '): ' + text);
});