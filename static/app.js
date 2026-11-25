const PERSIAN_RE = /[\u0600-\u06FF]/;
const FALLBACK_PALETTE = [
  "#10b981",
  "#38bdf8",
  "#f59e0b",
  "#a78bfa",
  "#fb7185",
  "#34d399",
  "#60a5fa",
  "#fbbf24",
];
const METRIC_PRIORITY = [
  "points",
  "goals",
  "late_goals",
  "clean_sheets",
  "minutes",
  "reds",
  "yellows",
  "formations",
  "goal_diff",
  "n",
];
const SCROLL_THRESHOLD_PX = 120;

const state = {
  history: [],
  teamColorMap: new Map(),
  isLoading: false,
  userScrolledUp: false,
};

const form = document.getElementById("ask-form");
const questionInput = document.getElementById("question-input");
const submitBtn = document.getElementById("submit-btn");
const spinner = document.getElementById("loading-spinner");
const resetBtn = document.getElementById("reset-btn");
const chatScroll = document.getElementById("chat-scroll");
const threadEl = document.getElementById("thread");
const emptyStateEl = document.getElementById("empty-state");
const jumpLatestBtn = document.getElementById("jump-latest-btn");

function isPersian(text) {
  return PERSIAN_RE.test(text || "");
}

function applyDirection(el, text) {
  el.classList.remove("rtl", "ltr");
  el.classList.add(isPersian(text) ? "rtl" : "ltr");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isNearBottom() {
  return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight <= SCROLL_THRESHOLD_PX;
}

function scrollToBottom(behavior = "smooth") {
  chatScroll.scrollTo({ top: chatScroll.scrollHeight, behavior });
  state.userScrolledUp = false;
  jumpLatestBtn.classList.add("hidden");
}

function autoResizeTextarea() {
  questionInput.style.height = "auto";
  questionInput.style.height = `${Math.min(questionInput.scrollHeight, 140)}px`;
}

async function loadTeamColors() {
  try {
    const resp = await fetch("/api/teams");
    if (!resp.ok) return;
    const data = await resp.json();
    const teams = Array.isArray(data.teams) ? data.teams : [];
    for (const team of teams) {
      if (!team.primary_color) continue;
      if (team.name) state.teamColorMap.set(String(team.name).trim().toLowerCase(), team.primary_color);
      if (team.name_fa) state.teamColorMap.set(String(team.name_fa).trim().toLowerCase(), team.primary_color);
    }
  } catch (err) {
    console.warn("Could not preload team colors:", err);
  }
}

function setLoading(loading) {
  state.isLoading = loading;
  submitBtn.disabled = loading;
  questionInput.disabled = loading;
  spinner.classList.toggle("hidden", !loading);
}

function extractOrderByColumns(sql) {
  if (!sql || typeof sql !== "string") return [];
  const match = sql.match(/\bORDER\s+BY\s+([\s\S]+?)(?:\s+LIMIT\b|\s+OFFSET\b|$)/i);
  if (!match) return [];

  return match[1]
    .split(",")
    .map((part) =>
      part
        .trim()
        .replace(/\b(?:ASC|DESC|NULLS\s+FIRST|NULLS\s+LAST)\b/gi, "")
        .trim()
    )
    .map((expr) => {
      const dotParts = expr.split(".");
      return dotParts[dotParts.length - 1].replace(/["`[\]]/g, "").trim().toLowerCase();
    })
    .filter(Boolean);
}

function renderSafeMarkdown(rawText) {
  const escaped = escapeHtml(rawText || "");
  const formatInline = (line) =>
    line
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

  const lines = escaped.split(/\r?\n/);
  const blocks = [];
  let listType = null;
  let listItems = [];
  let paraLines = [];

  const flushParagraph = () => {
    if (paraLines.length > 0) {
      blocks.push(`<p>${paraLines.map(formatInline).join("<br />")}</p>`);
      paraLines = [];
    }
  };

  const flushList = () => {
    if (listItems.length > 0 && listType) {
      const itemsHtml = listItems.map((item) => `<li>${formatInline(item)}</li>`).join("");
      blocks.push(`<${listType}>${itemsHtml}</${listType}>`);
      listItems = [];
      listType = null;
    }
  };

  for (const line of lines) {
    const ulMatch = line.match(/^\s*[-*•]\s+(.+)$/);
    const olMatch = line.match(/^\s*\d+[.)]\s+(.+)$/);

    if (ulMatch) {
      flushParagraph();
      if (listType && listType !== "ul") flushList();
      listType = "ul";
      listItems.push(ulMatch[1]);
    } else if (olMatch) {
      flushParagraph();
      if (listType && listType !== "ol") flushList();
      listType = "ol";
      listItems.push(olMatch[1]);
    } else if (line.trim() === "") {
      flushParagraph();
      flushList();
    } else {
      flushList();
      paraLines.push(line);
    }
  }

  flushParagraph();
  flushList();
  return blocks.join("") || "<p></p>";
}

function detectChartConfig(columns, rows, sql = "") {
  if (!Array.isArray(columns) || !Array.isArray(rows) || rows.length < 2 || columns.length < 2) {
    return null;
  }

  let labelIdx = -1;
  const numericIndices = [];

  for (let colIdx = 0; colIdx < columns.length; colIdx += 1) {
    const values = rows.map((r) => r[colIdx]).filter((v) => v !== null && v !== undefined && v !== "");
    if (values.length === 0) continue;

    const allNumeric = values.every((v) => typeof v === "number" && Number.isFinite(v));
    if (allNumeric) {
      const colName = String(columns[colIdx]).toLowerCase();
      if (!colName.endsWith("_id") && colName !== "id") {
        numericIndices.push(colIdx);
      }
    } else if (labelIdx === -1) {
      labelIdx = colIdx;
    }
  }

  if (labelIdx === -1 || numericIndices.length === 0) {
    return null;
  }

  const colLower = columns.map((c) => String(c).toLowerCase());
  let defaultMetricIdx = -1;

  for (const orderCol of extractOrderByColumns(sql)) {
    const found = numericIndices.find((idx) => colLower[idx] === orderCol);
    if (found !== undefined) {
      defaultMetricIdx = found;
      break;
    }
  }

  if (defaultMetricIdx === -1) {
    for (const metricName of METRIC_PRIORITY) {
      const found = numericIndices.find((idx) => colLower[idx] === metricName);
      if (found !== undefined) {
        defaultMetricIdx = found;
        break;
      }
    }
  }

  if (defaultMetricIdx === -1) {
    defaultMetricIdx = numericIndices[0];
  }

  const labels = rows.map((r) => String(r[labelIdx] ?? ""));
  const colors = labels.map((lbl, idx) => {
    const teamColor = state.teamColorMap.get(lbl.trim().toLowerCase());
    return teamColor || FALLBACK_PALETTE[idx % FALLBACK_PALETTE.length];
  });

  return {
    labelColumn: columns[labelIdx],
    numericIndices,
    defaultMetricIdx,
    metricColumn: columns[defaultMetricIdx],
    labels,
    values: rows.map((r) => Number(r[defaultMetricIdx] ?? 0)),
    colors,
  };
}

function downloadCsv(columns, rows) {
  const escapeCell = (cell) => {
    const str = cell === null || cell === undefined ? "" : String(cell);
    return `"${str.replace(/"/g, '""')}"`;
  };
  const lines = [columns.map(escapeCell).join(",")];
  for (const row of rows) {
    lines.push(row.map(escapeCell).join(","));
  }
  const blob = new Blob(["\uFEFF" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "football_query_results.csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function copyText(text, btnEl, defaultLabel) {
  navigator.clipboard.writeText(text).then(() => {
    btnEl.textContent = "Copied!";
    setTimeout(() => {
      btnEl.textContent = defaultLabel;
    }, 1500);
  });
}

function buildResultsPanel(data) {
  const panel = document.createElement("div");
  panel.className = "results-panel";

  const chartCfg = detectChartConfig(data.columns, data.rows, data.sql || "");
  let activeView = chartCfg ? "chart" : "table";

  const toolbar = document.createElement("div");
  toolbar.className = "results-toolbar";

  const tabs = document.createElement("div");
  tabs.className = "view-tabs";

  const chartTab = document.createElement("button");
  chartTab.type = "button";
  chartTab.className = `tab-btn ${activeView === "chart" ? "active" : ""}`;
  chartTab.textContent = "Chart";

  const tableTab = document.createElement("button");
  tableTab.type = "button";
  tableTab.className = `tab-btn ${activeView === "table" ? "active" : ""}`;
  tableTab.textContent = `Table (${data.row_count}${data.truncated ? "+" : ""})`;

  let metricSelect = null;
  if (chartCfg) {
    tabs.appendChild(chartTab);
  }
  tabs.appendChild(tableTab);

  if (chartCfg && chartCfg.numericIndices.length > 1) {
    metricSelect = document.createElement("select");
    metricSelect.className = "metric-select";
    metricSelect.setAttribute("aria-label", "Select chart metric");
    for (const idx of chartCfg.numericIndices) {
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = data.columns[idx];
      if (idx === chartCfg.defaultMetricIdx) {
        opt.selected = true;
      }
      metricSelect.appendChild(opt);
    }
    tabs.appendChild(metricSelect);
  }

  const csvBtn = document.createElement("button");
  csvBtn.type = "button";
  csvBtn.className = "btn btn-secondary btn-sm";
  csvBtn.textContent = "Download CSV";
  csvBtn.addEventListener("click", () => downloadCsv(data.columns, data.rows));

  toolbar.appendChild(tabs);
  toolbar.appendChild(csvBtn);
  panel.appendChild(toolbar);

  const chartWrap = document.createElement("div");
  chartWrap.className = `chart-wrap ${activeView === "chart" ? "" : "hidden"}`;
  const canvas = document.createElement("canvas");
  chartWrap.appendChild(canvas);
  panel.appendChild(chartWrap);

  const tableWrap = document.createElement("div");
  tableWrap.className = `table-scroll ${activeView === "table" ? "" : "hidden"}`;
  const table = document.createElement("table");
  table.className = "data-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const col of data.columns) {
    const th = document.createElement("th");
    th.textContent = col;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const row of data.rows) {
    const tr = document.createElement("tr");
    for (const cell of row) {
      const td = document.createElement("td");
      td.textContent = cell === null || cell === undefined ? "—" : String(cell);
      if (typeof cell === "string" && isPersian(cell)) {
        td.dir = "rtl";
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  panel.appendChild(tableWrap);

  if (chartCfg && typeof window.Chart === "function") {
    const chartInstance = new window.Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: chartCfg.labels,
        datasets: [
          {
            label: chartCfg.metricColumn,
            data: chartCfg.values,
            backgroundColor: chartCfg.colors,
            borderColor: "rgba(255, 255, 255, 0.25)",
            borderWidth: 1.5,
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            labels: { color: "#e6edf7" },
          },
        },
        scales: {
          x: {
            ticks: { color: "#93a4c3" },
            grid: { color: "rgba(36, 51, 82, 0.45)" },
          },
          y: {
            beginAtZero: true,
            ticks: { color: "#93a4c3" },
            grid: { color: "rgba(36, 51, 82, 0.45)" },
          },
        },
      },
    });

    if (metricSelect) {
      metricSelect.addEventListener("change", () => {
        const nextIdx = Number(metricSelect.value);
        chartInstance.data.datasets[0].label = data.columns[nextIdx];
        chartInstance.data.datasets[0].data = data.rows.map((r) => Number(r[nextIdx] ?? 0));
        chartInstance.update();
      });
    }

    chartTab.addEventListener("click", () => {
      activeView = "chart";
      chartTab.classList.add("active");
      tableTab.classList.remove("active");
      chartWrap.classList.remove("hidden");
      tableWrap.classList.add("hidden");
      if (metricSelect) metricSelect.classList.remove("hidden");
    });

    tableTab.addEventListener("click", () => {
      activeView = "table";
      tableTab.classList.add("active");
      chartTab.classList.remove("active");
      tableWrap.classList.remove("hidden");
      chartWrap.classList.add("hidden");
      if (metricSelect) metricSelect.classList.add("hidden");
    });
  }

  return panel;
}

function buildTracePanel(data) {
  const details = document.createElement("details");
  details.className = "trace-panel";
  details.open = true;

  const summary = document.createElement("summary");
  summary.textContent = "Explainability & Execution Trace";
  details.appendChild(summary);

  const tablesHtml = (data.tables_used || []).length
    ? data.tables_used.map((t) => `<span class="meta-pill">${escapeHtml(t)}</span>`).join("")
    : `<span class="meta-pill">none</span>`;

  const colsHtml = (data.columns_used || []).length
    ? data.columns_used.map((c) => `<span class="meta-pill">${escapeHtml(c)}</span>`).join("")
    : `<span class="meta-pill">none</span>`;

  const valChecked = data.validation?.checked;
  const valStatus = !valChecked
    ? "Skipped (no SQL)"
    : data.validation.valid
      ? "Passed"
      : `Failed: ${(data.validation.errors || []).join("; ")}`;

  const dryChecked = data.dry_run?.checked;
  let dryHtml = `<div class="dry-run-status">Skipped</div>`;
  if (dryChecked) {
    if (data.dry_run.ok) {
      const steps = (data.dry_run.plan && data.dry_run.plan.length > 0)
        ? data.dry_run.plan.join("\n")
        : "OK";
      dryHtml = `<div class="dry-run-status">Passed</div><pre class="dry-run-plan">${escapeHtml(steps)}</pre>`;
    } else {
      dryHtml = `<div class="dry-run-status">Failed</div>`;
    }
  }

  const grid = document.createElement("div");
  grid.className = "trace-grid";
  grid.innerHTML = `
    <div class="trace-item">
      <div class="trace-label">Intent</div>
      <div class="trace-value">${escapeHtml(data.intent || "—")}</div>
    </div>
    <div class="trace-item">
      <div class="trace-label">Action Taken</div>
      <div class="trace-value"><code>${escapeHtml(data.action)}</code></div>
    </div>
    <div class="trace-item">
      <div class="trace-label">Row Count</div>
      <div class="trace-value">${escapeHtml(data.row_count)}${data.truncated ? " (truncated at 200)" : ""}</div>
    </div>
    <div class="trace-item">
      <div class="trace-label">Validation Result</div>
      <div class="trace-value">${escapeHtml(valStatus)}</div>
    </div>
    <div class="trace-item">
      <div class="trace-label">Dry-Run (EXPLAIN QUERY PLAN)</div>
      <div class="trace-value">${dryHtml}</div>
    </div>
    <div class="trace-item">
      <div class="trace-label">Tables Used</div>
      <div class="trace-value pill-list">${tablesHtml}</div>
    </div>
    <div class="trace-item">
      <div class="trace-label">Columns Used</div>
      <div class="trace-value pill-list">${colsHtml}</div>
    </div>
  `;
  details.appendChild(grid);

  if (data.sql) {
    const sqlWrap = document.createElement("div");
    sqlWrap.className = "sql-block-wrap";

    const sqlHeader = document.createElement("div");
    sqlHeader.className = "sql-block-header";
    sqlHeader.innerHTML = `<span>Generated SQLite Query</span>`;

    const copySqlBtn = document.createElement("button");
    copySqlBtn.type = "button";
    copySqlBtn.className = "btn btn-secondary btn-sm";
    copySqlBtn.textContent = "Copy SQL";
    copySqlBtn.addEventListener("click", () => copyText(data.sql, copySqlBtn, "Copy SQL"));
    sqlHeader.appendChild(copySqlBtn);

    const pre = document.createElement("pre");
    pre.className = "sql-code";
    pre.textContent = data.sql;

    sqlWrap.appendChild(sqlHeader);
    sqlWrap.appendChild(pre);
    details.appendChild(sqlWrap);
  }

  const traceActions = document.createElement("div");
  traceActions.className = "trace-actions";
  const copyTraceBtn = document.createElement("button");
  copyTraceBtn.type = "button";
  copyTraceBtn.className = "btn btn-secondary btn-sm";
  copyTraceBtn.textContent = "Copy Trace JSON";
  copyTraceBtn.addEventListener("click", () => {
    copyText(JSON.stringify(data, null, 2), copyTraceBtn, "Copy Trace JSON");
  });
  traceActions.appendChild(copyTraceBtn);
  details.appendChild(traceActions);

  return details;
}

function createPendingTurn(question) {
  emptyStateEl.classList.add("hidden");

  const turn = document.createElement("article");
  turn.className = "turn";

  const userBubble = document.createElement("div");
  userBubble.className = "user-bubble";
  userBubble.textContent = question;
  applyDirection(userBubble, question);
  turn.appendChild(userBubble);

  const card = document.createElement("div");
  card.className = "assistant-card";
  card.innerHTML = `
    <div class="thinking-row">
      <span class="thinking-spinner" aria-hidden="true"></span>
      <span>Planning SQL, validating, and querying read-only DB...</span>
    </div>
  `;
  turn.appendChild(card);

  threadEl.appendChild(turn);
  return card;
}

function populateAssistantCard(card, data, errorMessage = null) {
  card.innerHTML = "";

  const header = document.createElement("div");
  header.className = "card-header";
  const badge = document.createElement("span");
  if (errorMessage) {
    badge.className = "action-badge badge-error";
    badge.textContent = "error";
  } else {
    badge.className = `action-badge badge-${data.action}`;
    badge.textContent = data.action;
  }
  header.appendChild(badge);
  card.appendChild(header);

  const answerEl = document.createElement("div");
  answerEl.className = "answer-text";
  const displayAnswer = errorMessage || data.answer || "";
  answerEl.innerHTML = renderSafeMarkdown(displayAnswer);
  applyDirection(answerEl, displayAnswer);
  card.appendChild(answerEl);

  if (!errorMessage && data.row_count > 0 && Array.isArray(data.columns) && Array.isArray(data.rows)) {
    card.appendChild(buildResultsPanel(data));
  }

  if (!errorMessage && data) {
    card.appendChild(buildTracePanel(data));
  }
}

async function submitQuestion(rawQuestion) {
  const question = rawQuestion.trim();
  if (!question || state.isLoading) return;

  const pendingCard = createPendingTurn(question);
  questionInput.value = "";
  autoResizeTextarea();
  applyDirection(questionInput, "");

  // Always scroll smoothly to the bottom when the user sends a message.
  scrollToBottom("smooth");
  setLoading(true);

  let hasChart = false;
  try {
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        history: state.history,
      }),
    });

    const payload = await resp.json();
    const shouldAutoScroll = !state.userScrolledUp && isNearBottom();

    if (!resp.ok) {
      populateAssistantCard(pendingCard, null, payload.detail || `Request failed (${resp.status})`);
    } else {
      populateAssistantCard(pendingCard, payload);
      state.history.push({ question, answer: payload.answer || "" });
      hasChart = Boolean(pendingCard.querySelector("canvas"));
    }

    if (shouldAutoScroll) {
      requestAnimationFrame(() => {
        chatScroll.scrollTop = chatScroll.scrollHeight;
        if (hasChart) {
          requestAnimationFrame(() => {
            chatScroll.scrollTop = chatScroll.scrollHeight;
          });
        }
      });
      jumpLatestBtn.classList.add("hidden");
    } else {
      jumpLatestBtn.classList.remove("hidden");
    }
  } catch (err) {
    const shouldAutoScroll = !state.userScrolledUp && isNearBottom();
    populateAssistantCard(pendingCard, null, `Network error: ${err.message}`);
    if (shouldAutoScroll) {
      requestAnimationFrame(() => {
        chatScroll.scrollTop = chatScroll.scrollHeight;
      });
      jumpLatestBtn.classList.add("hidden");
    } else {
      jumpLatestBtn.classList.remove("hidden");
    }
  } finally {
    setLoading(false);
    questionInput.focus();
  }
}

chatScroll.addEventListener("scroll", () => {
  if (isNearBottom()) {
    state.userScrolledUp = false;
    jumpLatestBtn.classList.add("hidden");
  } else {
    state.userScrolledUp = true;
  }
});

jumpLatestBtn.addEventListener("click", () => {
  scrollToBottom("smooth");
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  submitQuestion(questionInput.value);
});

questionInput.addEventListener("input", () => {
  applyDirection(questionInput, questionInput.value);
  autoResizeTextarea();
});

questionInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitQuestion(questionInput.value);
  }
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const q = chip.getAttribute("data-question") || "";
    submitQuestion(q);
  });
});

resetBtn.addEventListener("click", () => {
  state.history = [];
  state.userScrolledUp = false;
  threadEl.innerHTML = "";
  emptyStateEl.classList.remove("hidden");
  jumpLatestBtn.classList.add("hidden");
  questionInput.value = "";
  autoResizeTextarea();
  applyDirection(questionInput, "");
  questionInput.focus();
});

loadTeamColors();
