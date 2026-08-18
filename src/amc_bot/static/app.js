(() => {
  const dryRun = document.querySelector('#dry-run');
  const acknowledgement = document.querySelector('#live-ack');
  if (dryRun && acknowledgement) {
    const updateAcknowledgement = () => {
      acknowledgement.disabled = dryRun.checked;
      if (dryRun.checked) acknowledgement.value = '';
    };
    dryRun.addEventListener('change', updateAcknowledgement);
    updateAcknowledgement();
  }

  const status = document.querySelector('#watcher-status');
  const statusMessage = document.querySelector('#status-message');
  const statusDetail = document.querySelector('#status-detail');
  if (!status || !statusMessage || !statusDetail) return;

  const refreshStatus = async () => {
    try {
      const response = await fetch(status.dataset.statusUrl, {cache: 'no-store'});
      if (!response.ok) return;
      const value = await response.json();
      statusMessage.textContent = value.message || value.state || value.phase || 'Ready';
      const details = [];
      if (value.attempts) details.push(`attempt ${value.attempts}`);
      if (value.next_poll_seconds != null) details.push(`next check in ${value.next_poll_seconds}s`);
      if (value.selected_seats?.length) details.push(`seats ${value.selected_seats.join(', ')}`);
      statusDetail.textContent = details.length ? ` — ${details.join(' · ')}` : '';
    } catch (_error) {
      // A stopped local server needs no visible browser error.
    }
  };
  window.setInterval(refreshStatus, 2000);
})();
