// The server owns consent. This indicator only displays and revokes it.
export function createHostExecutionIndicator({ getSessionId, apiBase = () => '', fetchImpl = (...args) => fetch(...args) }) {
  let sequence = 0;
  const button = document.createElement('button');
  button.id = 'host-execution-indicator-btn';
  button.type = 'button';
  button.className = 'input-icon-btn tool-indicator active';
  button.textContent = 'Without isolation · Revoke';
  button.title = 'Commands in this chat run with the OS account permissions, including outside the workspace. Click to revoke for future commands.';
  button.setAttribute('aria-label', 'Revoke commands without isolation for this chat');
  button.style.display = 'none';
  document.getElementById('workspace-indicator-btn')?.parentElement?.appendChild(button);

  async function refresh(workspace) {
    const sessionId = getSessionId();
    const current = ++sequence;
    button.style.display = 'none';
    if (!sessionId) return;
    try {
      const query = workspace === undefined ? '' : '?workspace=' + encodeURIComponent(workspace);
      const response = await fetchImpl(`${apiBase()}/api/session/${encodeURIComponent(sessionId)}/host-execution${query}`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('Could not read chat execution permission');
      const status = await response.json();
      if (current !== sequence || getSessionId() !== sessionId) return;
      button.textContent = 'Without isolation · Revoke';
      button.style.display = status.enabled ? 'inline-flex' : 'none';
      button.disabled = false;
    } catch (error) {
      if (current !== sequence || getSessionId() !== sessionId) return;
      // A failed status read must not silently hide a possibly active grant.
      button.style.display = 'inline-flex';
      button.textContent = 'Execution permission unavailable · Revoke';
      button.disabled = false;
    }
  }

  button.addEventListener('click', async () => {
    const sessionId = getSessionId();
    if (!sessionId) return;
    button.disabled = true;
    try {
      const response = await fetchImpl(`${apiBase()}/api/session/${encodeURIComponent(sessionId)}/host-execution`, { method: 'DELETE', credentials: 'same-origin' });
      if (!response.ok) throw new Error('Revocation failed');
      await refresh();
    } catch (error) {
      if (getSessionId() !== sessionId) return;
      button.disabled = false;
      button.textContent = 'Revocation failed · Retry';
      button.style.display = 'inline-flex';
    }
  });

  window.addEventListener('odysseus:session-changed', () => refresh());
  window.addEventListener('odysseus:workspace-changed', event => refresh(event.detail?.path || ''));
  window.addEventListener('focus', () => refresh());
  return { refresh };
}
