const status = document.querySelector('#status'), error = document.querySelector('#error');
const select = document.querySelector('#scenario');
async function call(path, body) {
  error.textContent = '';
  const buttons = [...document.querySelectorAll('button')];
  buttons.forEach(b => b.disabled = true);
  try {
    const response = await fetch('/api/testing/' + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Canvas-Hub': '1'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || result.error);
    select.value = result.scenario;
    status.textContent = `Selected fixture: ${result.scenario}\nSource: ${result.source_status}\nLocal fixture reads: ${result.reads}\nNo external network or AI calls.`;
  } catch (e) { error.textContent = e.message; }
  finally { buttons.forEach(b => b.disabled = false); }
}
document.querySelector('#select').onclick = () => call('scenario', {scenario: select.value});
document.querySelector('#refresh').onclick = () => call('refresh', {});
document.querySelector('#propose').onclick = () => call('propose', {});
call('demo');
