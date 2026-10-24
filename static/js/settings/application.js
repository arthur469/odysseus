// Native desktop server controls. Availability is decided by the server.
export function initApplicationControls({ root = document, fetchImpl = fetch, confirm }) {
  const card = root.getElementById('settings-application-card');
  const button = root.getElementById('settings-application-stop');
  const message = root.getElementById('settings-application-message');
  if (!card || !button || !message) return;

  fetchImpl('/api/application/status', { credentials: 'same-origin', cache: 'no-store' })
    .then(response => response.ok ? response.json() : null)
    .then(status => {
      if (!status?.can_stop) return;
      card.classList.remove('hidden');
      button.disabled = !!status.stopping;
    }).catch(() => {});

  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    const accepted = await confirm(
      'Active conversations and tasks may be interrupted. To restart Odysseus, use its desktop shortcut.',
      { title: 'Stop Odysseus?', confirmText: 'Stop', cancelText: 'Cancel', danger: true },
    );
    if (!accepted) { button.disabled = false; return; }
    message.textContent = 'Stopping…';
    try {
      const response = await fetchImpl('/api/application/stop', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmation: 'stop-odysseus' }),
      });
      if (!response.ok) throw new Error('Stop request refused. Try again from this PC with an administrator account.');
      message.textContent = 'Stop requested. You can close this tab. To restart Odysseus, use the desktop shortcut.';
    } catch (error) {
      message.textContent = error.message || 'Unable to request a stop. Check the connection.';
      button.disabled = false;
    }
  });
}
