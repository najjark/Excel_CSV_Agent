const API = "http://127.0.0.1:5000";
let abortController = null;
let fileCache = {}; // Stores metadata so we don't spam the server on every hover
let tooltipTimeout; // Add this at the top

// ── Drag and Drop ──
const uploadZone = document.getElementById("upload-zone");
uploadZone.addEventListener("dragover", e => { e.preventDefault(); uploadZone.style.borderColor = "var(--accent)"; });
uploadZone.addEventListener("dragleave", () => { uploadZone.style.borderColor = "var(--border)"; });
uploadZone.addEventListener("drop", async e => {
    e.preventDefault();
    uploadZone.style.borderColor = "var(--border)";
    for (const file of e.dataTransfer.files) await uploadFile(file);
});

document.getElementById("file-input").addEventListener("change", async e => {
    for (const file of e.target.files) await uploadFile(file);
    e.target.value = "";
});

document.getElementById("upload-zone").addEventListener("click", () => {
    document.getElementById("file-input").click();
});

// ── File Upload ──
async function uploadFile(file) {
    const incomingBaseName = file.name.trim().replace(/\.[^/.]+$/, "").replace(/\s+/g, '_');

    const ext = file.name.split(".").pop().toLowerCase();
    if (!["csv", "xlsx", "xls"].includes(ext)) {
        showWarning("Only CSV and Excel files are supported.");
        return;
    }

    const existingBadge = document.getElementById(`badge-${incomingBaseName}`);
    if (existingBadge) {
        existingBadge.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        existingBadge.classList.remove("highlight-exist");
        void existingBadge.offsetWidth;
        existingBadge.classList.add("highlight-exist");
        setTimeout(() => existingBadge.classList.remove("highlight-exist"), 800);
        return;
    }

    const formData = new FormData();
    formData.append("file", file);
    try {
        const res = await fetch(`${API}/upload`, {
            method: "POST",
            body: formData,
            credentials: "include"
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        addFileBadge(data.name, data.rows, data.cols);
    } catch (e) {
        alert(`Failed to upload ${file.name}: ${e.message}`);
    }
}

function addFileBadge(name, rows, cols) {
    const container = document.getElementById("file-list");
    const placeholder = container.querySelector(".empty-files-message");
    if (placeholder) placeholder.remove();

    const badge = document.createElement("div");
    badge.className = "file-badge";
    badge.id = `badge-${name}`;
    
    badge.onmouseenter = () => handleHover(name, badge);
    badge.onmouseleave = hideTooltip;

    // Notice the onclick added to the badge itself
    badge.onclick = () => previewFile(name);

    badge.innerHTML = `
        <span class="file-name">${name}</span>
        <span class="file-meta">${rows} rows · ${cols} cols</span>
        <button class="file-remove" onclick="removeFile('${name}', event)">✕</button>
    `;
    container.appendChild(badge);
}

async function removeFile(name, event) {
    event.stopPropagation();
    await fetch(`${API}/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
        credentials: "include"
    });
    document.getElementById(`badge-${name}`)?.remove();

    if (!document.querySelector(".file-badge")) {
        document.getElementById("file-list").innerHTML = `<div class="empty-files-message">No files uploaded</div>`;
    }
}

// ── Ask Question ──
async function askQuestion() {
    const input = document.getElementById("question-input");
    const question = input.value.trim();
    if (!question) return;

    if (!document.querySelector(".file-badge")) {
        const uploadZone = document.getElementById("upload-zone");
        uploadZone.classList.remove("nudge-active");
        void uploadZone.offsetWidth;
        uploadZone.classList.add("nudge-active");
        setTimeout(() => uploadZone.classList.remove("nudge-active"), 400);
        return;
    }

    input.value = "";
    const btn = document.getElementById("ask-btn");
    btn.disabled = true;
    input.disabled = true;

    // Hide empty state
    const emptyMsg = document.querySelector(".empty-results-message");
    if (emptyMsg) emptyMsg.style.display = "none";

    const area = document.getElementById("results-area");

    // Show question bubble immediately
    const questionPreview = document.createElement("div");
    questionPreview.className = "result-card";
    questionPreview.id = "question-preview";
    questionPreview.innerHTML = `<div class="question-bubble">${escapeHtml(question)}</div>`;

    // Show thinking card below question
    abortController = new AbortController();
    const thinking = document.createElement("div");
    thinking.className = "thinking-card";
    thinking.id = "thinking-state";
    thinking.innerHTML = `
        <div class="spinner"></div>
        <div class="thinking-text">Analysing...</div>
        <button class="cancel-btn" onclick="cancelRequest()">✕</button>
    `;

    area.appendChild(questionPreview);
    area.appendChild(thinking);

    try {
        const res = await fetch(`${API}/ask`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question }),
            credentials: "include",
            signal: abortController.signal
        });

        const data = await res.json();

        document.getElementById("question-preview")?.remove();
        document.getElementById("thinking-state")?.remove();

        if (data.error === "Please ask a question related to your data.") {
            showWarning("I'm a Data Agent! Please ask a question related to your files.");
            btn.disabled = false;
            input.disabled = false;
            input.focus();
            return;
        }

        addResult(question, data.result, data.code, data.error);

    } catch (e) {
        document.getElementById("question-preview")?.remove();
        document.getElementById("thinking-state")?.remove();
        if (e.name !== "AbortError") {
            addResult(question, null, null, e.message);
        }
    } finally {
        btn.disabled = false;
        input.disabled = false;
        input.focus();
    }
}

function cancelRequest() {
    if (abortController) {
        abortController.abort();
        abortController = null;
    }
    document.getElementById("question-preview")?.remove();
    document.getElementById("thinking-state")?.remove();
    if (!document.querySelector(".result-card")) {
        const emptyMsg = document.querySelector(".empty-results-message");
        if (emptyMsg) emptyMsg.style.display = "flex";
    }
}

// ── Results ──
function addResult(question, result, code, error) {
    const card = document.createElement("div");
    card.className = "result-card";

    const isInvalid = error === "Please ask a question related to your data.";
    let resultHTML = "";

    if (isInvalid) {
        resultHTML = `<div class="invalid-answer">${error}</div>`;
    } else if (error) {
        resultHTML = `<div class="error-answer">Error: ${error}</div>`;
    } else if (Array.isArray(result) && result.length && typeof result[0] === "object") {
        resultHTML = buildTable(result);
    } else if (Array.isArray(result)) {
        resultHTML = buildList(result);
    } else {
        resultHTML = `<div class="result-answer">${result}</div>`;
    }

    card.innerHTML = `
        <div class="question-bubble">${escapeHtml(question)}</div>
        <div class="answer-bubble">
            <button class="bubble-remove" onclick="removeResult(this)">✕</button>
            ${resultHTML}
            ${(code && !isInvalid) ? `
            <details class="code-accordion">
                <summary>View analysis logic</summary>
                <div class="code-block">
                    <div class="code-header">
                        <span>Generated Code</span>
                    </div>
                    <pre>${escapeHtml(code)}</pre>
                </div>
            </details>
            ` : ""}
        </div>
    `;

    const area = document.getElementById("results-area");

    area.appendChild(card);
}

function removeResult(btn) {
    btn.closest(".result-card").remove();
    if (!document.querySelector(".result-card")) {
        const emptyMsg = document.querySelector(".empty-results-message");
        if (emptyMsg) emptyMsg.style.display = "flex";
    }
}

// ── Build Helpers ──
function buildTable(data) {
    if (!data.length) return `<div class="result-answer">No results found.</div>`;
    const headers = Object.keys(data[0]);
    const headerRow = headers.map(h => `<th>${h}</th>`).join("");
    const rows = data.map(row => {
        const cells = headers.map(h => {
            const val = row[h] ?? "";
            const displayVal = (typeof val === 'string' && val.length > 40)
                ? val.substring(0, 37) + "..."
                : val;
            return `<td title="${escapeHtml(String(val))}">${escapeHtml(String(displayVal))}</td>`;
        }).join("");
        return `<tr>${cells}</tr>`;
    }).join("");
    return `<div class="table-container"><table class="result-table"><thead><tr>${headerRow}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function buildList(data) {
    if (!data.length) return `<div class="result-answer">No results found.</div>`;

    if (data.length === 1) {
        return `<div class="result-answer">${escapeHtml(String(data[0]))}</div>`;
    }

    if (data.length <= 10) {
        const items = data.map(v => `<span class="list-chip">${escapeHtml(String(v))}</span>`).join("");
        return `<div class="result-chips">${items}</div>`;
    }

    if (data.length <= 50) {
        const items = data.map(v => `<div class="list-item">${escapeHtml(String(v))}</div>`).join("");
        return `<div class="result-grid">${items}</div>`;
    }

    const items = data.map(v => `<div class="list-item">${escapeHtml(String(v))}</div>`).join("");
    const id = `list-${Date.now()}`;
    return `
        <div id="${id}" class="result-grid scrollable">${items}</div>
        ${data.length > 30 ? `<button class="expand-btn" onclick="toggleList('${id}', this)">Show all (${data.length})</button>` : ""}
    `;
}

function escapeHtml(str) {
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ── UI Helpers ──
function toggleHelp() {
    const help = document.getElementById("help-section");
    const btn = document.querySelector(".help-btn");
    const isOpen = help.style.display !== "none";
    help.style.display = isOpen ? "none" : "block";
    btn.textContent = isOpen ? "?" : "✕";
}

function toggleList(id, btn) {
    const list = document.getElementById(id);
    if (list.classList.contains("scrollable")) {
        list.classList.remove("scrollable");
        btn.textContent = "Show less";
    } else {
        list.classList.add("scrollable");
        btn.textContent = "Show all";
        list.scrollIntoView({ behavior: "smooth", block: "start" });
    }
}

function showWarning(message) {
    const toast = document.createElement("div");
    toast.className = "toast-notification";
    toast.innerHTML = `⚠️ ${message}`;
    document.body.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = "0";
        toast.style.transition = "opacity 0.5s ease";
        setTimeout(() => toast.remove(), 500);
    }, 4000);
}


async function handleHover(name, element) {
    clearTimeout(tooltipTimeout);

    // 1. Fetch data only if we don't have it[cite: 1, 2]
    if (!fileCache[name]) {
        try {
            const res = await fetch(`${API}/inspect`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
                credentials: "include"
            });
            const data = await res.json();
            if (data.error) return;
            fileCache[name] = data;
        } catch (e) { return; }
    }

    const data = fileCache[name];

    // 2. Create the floating window
    let tooltip = document.getElementById("file-tooltip") || document.createElement("div");
    tooltip.id = "file-tooltip";
    tooltip.className = "floating-tooltip";
    
    const cols = Object.entries(data.dtypes)
        .map(([c, t]) => `<div class="tooltip-row"><strong>${c}</strong> <span>${t}</span></div>`)
        .join("");

    tooltip.innerHTML = `
        <div class="tooltip-header">${name}</div>
        ${cols}
        <div class="tooltip-footer">Click to preview data</div>
    `;

    document.body.appendChild(tooltip);

    // 3. Position it relative to the pill
    const rect = element.getBoundingClientRect();
    tooltip.style.left = `${rect.left}px`;

    // THE FIX: Reduce the gap to 2px so the mouse doesn't "leave" the zone
    tooltip.style.top = `${rect.top - tooltip.offsetHeight + 1}px`;

    // NEW: Let the tooltip stay open if the mouse is OVER the tooltip itself
    tooltip.onmouseenter = () => clearTimeout(tooltipTimeout);
    tooltip.onmouseleave = hideTooltip;
}

function hideTooltip() {
    tooltipTimeout = setTimeout(() => {
        document.getElementById("file-tooltip")?.remove();
    }, 600);
}

async function previewFile(name) {
    hideTooltip(); // Close tooltip when modal opens
    
    // Check cache directly instead of calling handleHover
    if (!fileCache[name]) {
        try {
            const res = await fetch(`${API}/inspect`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
                credentials: "include"
            });
            fileCache[name] = await res.json();
        } catch (e) { return; }
    }

    const data = fileCache[name];
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.onclick = () => modal.remove(); // Click background to close

    const content = document.createElement("div");
    content.className = "modal-content";
    content.onclick = (e) => e.stopPropagation(); // Don't close when clicking table

    // Reuse your buildTable helper![cite: 2]
    content.innerHTML = `
        <div class="modal-header">
            <h3>Preview: ${name}</h3>
            <button onclick="this.closest('.modal-overlay').remove()">✕</button>
        </div>
        <div class="modal-body">${buildTable(data.preview)}</div>
    `;

    modal.appendChild(content);
    document.body.appendChild(modal);
}

// ── Event Listeners ──
document.getElementById("question-input").addEventListener("keydown", e => {
    if (e.key === "Enter") {
        const btn = document.getElementById("ask-btn");
        btn.style.transform = "scale(0.95)";
        setTimeout(() => btn.style.transform = "scale(1)", 100);
        askQuestion();
    }
});

document.querySelectorAll(".help-chip").forEach(chip => {
    chip.addEventListener("click", () => {
        document.getElementById("question-input").value = chip.textContent;
        document.getElementById("question-input").focus();
        toggleHelp();
    });
});