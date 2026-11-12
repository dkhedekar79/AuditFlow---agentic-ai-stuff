/* AuditFlow — static preview of the review workflow.
   The data mirrors the four bundled receipts; the Python app remains the
   source of truth. No dependencies, no network requests. */

const SAMPLES = {
  clean: {
    file: "01_clean_software_receipt.txt",
    vendor: "Northwind Cloud Services",
    amount: 89,
    currency: "USD",
    date: "2026-10-14",
    category: "Software",
    confidence: 0.99,
    status: "AUTO_APPROVED",
    flags: [],
    checks: [
      { title: "Category budget", passed: true, message: "Software spend of $89.00 is within the $2,500.00 category limit ($2,411.00 remaining)." },
      { title: "Approved vendor", passed: true, message: "Northwind Cloud Services is an approved vendor (risk: low, terms: Net 30)." },
      { title: "Duplicate invoice", passed: true, message: "No earlier audit matches this vendor, date and amount." },
      { title: "Approval ceiling", passed: true, message: "Total of $89.00 is at or below the $500.00 auto-approval threshold." }
    ],
    trace: [
      ["system", "audit_started", "Auditing 01_clean_software_receipt.txt with the deterministic demo extractor."],
      ["extraction", "heuristic_extraction", "Parsed Northwind Cloud Services for $89.00 USD with 99% confidence."],
      ["tool", "policy.check_budget_limit", "Software spend of $89.00 is within the $2,500.00 category limit."],
      ["tool", "policy.verify_approved_vendor", "Northwind Cloud Services is an approved vendor."],
      ["tool", "policy.check_duplicate_invoice", "No earlier audit matches this vendor, date and amount."],
      ["tool", "policy.check_auto_approve_threshold", "Total of $89.00 is at or below the $500.00 threshold."],
      ["routing", "routing_decision", "Routed to AUTO_APPROVED with no audit flags."]
    ]
  },
  travel: {
    file: "02_over_limit_travel.json",
    vendor: "Contoso Travel Group",
    amount: 1249,
    currency: "USD",
    date: "2026-11-02",
    category: "Travel",
    confidence: 0.98,
    status: "PENDING_REVIEW",
    flags: ["Budget limit exceeded", "Above approval ceiling"],
    checks: [
      { title: "Category budget", passed: false, message: "Travel spend of $1,249.00 exceeds the $800.00 category limit by $449.00." },
      { title: "Approved vendor", passed: true, message: "Contoso Travel Group is an approved vendor (risk: low, terms: Net 15)." },
      { title: "Duplicate invoice", passed: true, message: "No earlier audit matches this vendor, date and amount." },
      { title: "Approval ceiling", passed: false, message: "Total of $1,249.00 exceeds the $500.00 auto-approval threshold and needs a human decision." }
    ],
    trace: [
      ["system", "audit_started", "Auditing 02_over_limit_travel.json with the deterministic demo extractor."],
      ["extraction", "heuristic_extraction", "Parsed Contoso Travel Group for $1,249.00 USD with 98% confidence."],
      ["tool", "policy.check_budget_limit", "Travel spend exceeds the $800.00 category limit by $449.00."],
      ["tool", "policy.verify_approved_vendor", "Contoso Travel Group is an approved vendor."],
      ["tool", "policy.check_duplicate_invoice", "No earlier audit matches this vendor, date and amount."],
      ["tool", "policy.check_auto_approve_threshold", "Total of $1,249.00 exceeds the $500.00 auto-approval threshold."],
      ["routing", "routing_decision", "Routed to PENDING_REVIEW with flags: BUDGET_LIMIT_EXCEEDED, OVER_AUTO_APPROVAL_THRESHOLD."]
    ]
  },
  duplicate: {
    file: "03_duplicate_software_receipt.txt",
    vendor: "Northwind Cloud Services",
    amount: 89,
    currency: "USD",
    date: "2026-10-14",
    category: "Software",
    confidence: 0.99,
    status: "PENDING_REVIEW",
    flags: ["Duplicate invoice"],
    checks: [
      { title: "Category budget", passed: true, message: "Software spend of $89.00 is within the $2,500.00 category limit." },
      { title: "Approved vendor", passed: true, message: "Northwind Cloud Services is an approved vendor." },
      { title: "Duplicate invoice", passed: false, message: "Possible duplicate: audit #1 already covers Northwind Cloud Services on 2026-10-14 for $89.00." },
      { title: "Approval ceiling", passed: true, message: "Total of $89.00 is at or below the $500.00 auto-approval threshold." }
    ],
    trace: [
      ["system", "audit_started", "Auditing 03_duplicate_software_receipt.txt with the deterministic demo extractor."],
      ["extraction", "heuristic_extraction", "Parsed Northwind Cloud Services for $89.00 USD with 99% confidence."],
      ["tool", "policy.check_budget_limit", "Software spend is within the category limit."],
      ["tool", "policy.verify_approved_vendor", "Northwind Cloud Services is an approved vendor."],
      ["tool", "policy.check_duplicate_invoice", "Audit #1 already covers this vendor, date and amount."],
      ["tool", "policy.check_auto_approve_threshold", "Total of $89.00 is below the auto-approval threshold."],
      ["routing", "routing_decision", "Routed to PENDING_REVIEW with flags: DUPLICATE_INVOICE."]
    ]
  },
  vendor: {
    file: "04_unapproved_vendor_receipt.json",
    vendor: "QuickCash Gadgets Ltd",
    amount: 249,
    currency: "USD",
    date: "2026-11-06",
    category: "Office supplies",
    confidence: 0.98,
    status: "PENDING_REVIEW",
    flags: ["Unapproved vendor"],
    checks: [
      { title: "Category budget", passed: true, message: "Office supplies spend of $249.00 is within the $300.00 category limit." },
      { title: "Approved vendor", passed: false, message: "QuickCash Gadgets Ltd is on the blocked-vendor list: failed the 2025 supplier security review." },
      { title: "Duplicate invoice", passed: true, message: "No earlier audit matches this vendor, date and amount." },
      { title: "Approval ceiling", passed: true, message: "Total of $249.00 is at or below the $500.00 auto-approval threshold." }
    ],
    trace: [
      ["system", "audit_started", "Auditing 04_unapproved_vendor_receipt.json with the deterministic demo extractor."],
      ["extraction", "heuristic_extraction", "Parsed QuickCash Gadgets Ltd for $249.00 USD with 98% confidence."],
      ["tool", "policy.check_budget_limit", "Office supplies spend is within the category limit."],
      ["tool", "policy.verify_approved_vendor", "QuickCash Gadgets Ltd is on the blocked-vendor list."],
      ["tool", "policy.check_duplicate_invoice", "No earlier audit matches this vendor, date and amount."],
      ["tool", "policy.check_auto_approve_threshold", "Total of $249.00 is below the auto-approval threshold."],
      ["routing", "routing_decision", "Routed to PENDING_REVIEW with flags: UNAPPROVED_VENDOR."]
    ]
  }
};

const STATUS_LABELS = {
  PENDING_REVIEW: "Needs review",
  AUTO_APPROVED: "Auto-approved",
  APPROVED: "Approved",
  REJECTED: "Rejected"
};

const decisions = {};
let activeKey = "clean";

const elements = {
  status: document.querySelector("#demo-status"),
  statusLabel: document.querySelector("#demo-status-label"),
  file: document.querySelector("#demo-file"),
  vendor: document.querySelector("#demo-vendor"),
  amount: document.querySelector("#demo-amount"),
  meta: document.querySelector("#demo-meta"),
  flags: document.querySelector("#demo-flags"),
  checks: document.querySelector("#demo-check-list"),
  trace: document.querySelector("#demo-trace"),
  note: document.querySelector("#demo-note-input"),
  noteWrap: document.querySelector("#demo-note-wrap"),
  approve: document.querySelector("#demo-approve"),
  reject: document.querySelector("#demo-reject"),
  reset: document.querySelector("#demo-reset"),
  decision: document.querySelector("#demo-decision")
};

const sampleButtons = Array.from(document.querySelectorAll("[data-sample]"));

function money(value, currency) {
  const amount = value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
  return currency === "USD" ? `$${amount}` : `${currency} ${amount}`;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function currentStatus(key) {
  return decisions[key] ? decisions[key].status : SAMPLES[key].status;
}

function currentTrace(key) {
  const sample = SAMPLES[key];
  const trace = sample.trace.slice();
  const decision = decisions[key];
  if (decision) {
    trace.push([
      "human",
      "human_decision",
      decision.note
        ? `Reviewer recorded ${decision.status.toLowerCase()}. Note: ${decision.note}`
        : `Reviewer recorded ${decision.status.toLowerCase()}.`
    ]);
  }
  return trace;
}

function renderChecks(sample) {
  elements.checks.innerHTML = sample.checks
    .map((check) => {
      const state = check.passed ? "Passed" : "Failed";
      return `
        <li>
          <span class="check-dot ${check.passed ? "" : "is-fail"}"></span>
          <div class="check-copy">
            <div class="check-head">
              <span class="check-title">${escapeHtml(check.title)}</span>
              <span class="check-state ${check.passed ? "" : "is-fail"}">${state}</span>
            </div>
            <p class="check-message">${escapeHtml(check.message)}</p>
          </div>
        </li>`;
    })
    .join("");
}

function renderTrace(key) {
  elements.trace.innerHTML = currentTrace(key)
    .map(
      (step, index) => `
        <li>
          <span class="trace-index">${String(index + 1).padStart(2, "0")}</span>
          <span>
            <span class="trace-name">${escapeHtml(step[1])}</span>
            <span class="trace-summary">${escapeHtml(step[2])}</span>
          </span>
        </li>`
    )
    .join("");
}

function render() {
  const sample = SAMPLES[activeKey];
  const status = currentStatus(activeKey);
  const decision = decisions[activeKey];

  elements.status.dataset.status = status;
  elements.statusLabel.textContent = STATUS_LABELS[status];
  elements.file.textContent = sample.file;
  elements.vendor.textContent = sample.vendor;
  elements.amount.textContent = money(sample.amount, sample.currency);
  elements.meta.textContent = `${sample.date} · ${sample.category} · ${Math.round(
    sample.confidence * 100
  )}% confidence`;

  elements.flags.innerHTML = sample.flags
    .map((flag) => `<li>${escapeHtml(flag)}</li>`)
    .join("");
  elements.flags.hidden = sample.flags.length === 0;

  renderChecks(sample);
  renderTrace(activeKey);

  const pending = status === "PENDING_REVIEW" && !decision;
  elements.approve.disabled = !pending;
  elements.reject.disabled = !pending;
  elements.note.disabled = !pending;
  elements.noteWrap.hidden = !pending;
  elements.reset.hidden = !decision;

  if (decision) {
    elements.decision.textContent = decision.note
      ? `Decision recorded: ${STATUS_LABELS[decision.status]} — ${decision.note}`
      : `Decision recorded: ${STATUS_LABELS[decision.status]}.`;
  } else if (status === "AUTO_APPROVED") {
    elements.decision.textContent = "All checks passed. No review required.";
  } else {
    elements.decision.textContent = "Waiting for a reviewer.";
  }

  sampleButtons.forEach((button) => {
    const selected = button.dataset.sample === activeKey;
    button.setAttribute("aria-selected", String(selected));
    button.setAttribute("aria-pressed", String(selected));
  });
}

function selectSample(key) {
  activeKey = key;
  elements.note.value = "";
  render();
}

function recordDecision(status) {
  const note = elements.note.value.trim();
  decisions[activeKey] = {
    status,
    note: note || null
  };
  elements.note.value = "";
  render();
}

sampleButtons.forEach((button) => {
  button.addEventListener("click", () => selectSample(button.dataset.sample));
});

elements.approve.addEventListener("click", () => recordDecision("APPROVED"));
elements.reject.addEventListener("click", () => recordDecision("REJECTED"));
elements.reset.addEventListener("click", () => {
  delete decisions[activeKey];
  render();
});

render();
