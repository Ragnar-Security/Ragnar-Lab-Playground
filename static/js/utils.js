// ============ UTILITY FUNCTIONS ============

// Escape a value for an HTML text node or a quoted attribute. Every value
// that came from the server (names, labels, errors) must pass through this
// before it is interpolated into an innerHTML string: those values originate
// with students and Canvas imports, and markup that runs in an admin session
// acts as the admin. Not for URL or JS contexts; build those with DOM APIs
// and addEventListener instead.
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
}

// `message` is trusted markup built by the caller; escape any interpolated
// server data with escapeHtml() before passing it in.
// Helper function to show messages
function showMessage(elementId, message, type = 'success') {
    const resultDiv = document.getElementById(elementId);
    const alertClass = type === 'error' ? 'alert-danger' : type === 'warning' ? 'alert-warning' : 'alert-success';

    resultDiv.innerHTML = `<div class="alert ${alertClass} alert-dismissible fade show" role="alert">
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    </div>`;

    // Message will stay until next action or manually dismissed
}

function showLoading(elementId) {
    const resultDiv = document.getElementById(elementId);
    resultDiv.innerHTML = `<div class="text-center">
        <div class="spinner-border text-primary" role="status">
            <span class="visually-hidden">Loading...</span>
        </div>
    </div>`;
}

// Format a date string in Eastern Time (EST/EDT)
function formatDateEST(dateStr) {
    if (!dateStr) return 'N/A';
    return new Date(dateStr).toLocaleString('en-US', {
        timeZone: 'America/New_York',
        year: 'numeric', month: 'numeric', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        timeZoneName: 'short'
    });
}

// Countdown timer management
window.countdownTimers = [];

function clearCountdownTimers() {
    window.countdownTimers.forEach(id => clearInterval(id));
    window.countdownTimers = [];
}

function startCountdownTimers() {
    document.querySelectorAll('.countdown-timer').forEach(span => {
        let remaining = parseInt(span.dataset.remaining, 10);
        if (isNaN(remaining) || remaining <= 0) {
            span.textContent = 'EXPIRED';
            span.className = 'badge bg-danger ms-1';
            return;
        }

        function render() {
            if (remaining <= 0) {
                span.textContent = 'EXPIRED';
                span.className = 'badge bg-danger ms-1';
                return false;
            }
            const h = Math.floor(remaining / 3600);
            const m = Math.floor((remaining % 3600) / 60);
            const s = remaining % 60;
            span.textContent = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
            span.className = remaining < 600 ? 'badge bg-danger ms-1' :
                             remaining < 1800 ? 'badge bg-warning text-dark ms-1' :
                             'badge bg-success ms-1';
            return true;
        }

        render();
        const id = setInterval(() => { remaining--; if (!render()) clearInterval(id); }, 1000);
        window.countdownTimers.push(id);
    });
}
