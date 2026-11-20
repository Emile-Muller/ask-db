// Ask DB showcase: renders the pre-recorded evaluation run in results.json.
// All text from the file is inserted with textContent, never as HTML.
(function () {
  "use strict";

  var CATEGORIES = {
    valid: { label: "Real questions", desc: "Ordinary business questions that should get an answer." },
    destructive: { label: "Delete or change data", desc: "Requests to wipe, edit or drop data." },
    prompt_injection: { label: "Prompt injection", desc: "Text trying to trick the AI into ignoring its rules." },
    stacked_sql: { label: "Hidden extra commands", desc: "A harmless query with a dangerous one tucked behind it." },
    exfiltration: { label: "Snooping", desc: "Attempts to read system details or files on the server." },
    sensitive_data: { label: "Private data", desc: "Requests for personal details or restricted text." },
    resource_abuse: { label: "Overload attempt", desc: "Queries built to make the database grind to a halt." },
    out_of_scope: { label: "Out of scope", desc: "Questions this historical data cannot answer." }
  };

  var LAYERS = {
    input_screen: { n: 1, label: "Input screen" },
    generation: { n: 2, label: "AI proposal" },
    validator: { n: 3, label: "SQL validator" },
    executor: { n: 4, label: "Locked-down run" },
    output_check: { n: 5, label: "Output check" }
  };

  var CHECKS = {
    parse: "Valid SQL",
    single_statement: "One statement only",
    no_comments: "No hidden comments",
    select_only: "Read-only (SELECT)",
    no_forbidden_nodes: "No forbidden operations",
    function_allowlist: "Approved functions only",
    table_allowlist: "Approved tables only",
    column_allowlist: "No restricted columns",
    complexity: "Not too heavy",
    limit: "Row limit"
  };

  var VERDICTS = {
    correct: { label: "Correct", tone: "ok" },
    partially_correct: { label: "Partly correct", tone: "warn" },
    incorrect: { label: "Incorrect", tone: "block" },
    cannot_tell: { label: "Can't tell", tone: "neutral" }
  };

  var REVIEW_OF = {
    answer: "of the answer",
    refusal: "of the AI's refusal",
    sql_only: "of the pasted SQL"
  };

  var STATUS = { pass: "✓ Passed", fail: "✗ Stopped", skipped: "– Not reached" };

  var state = { outcome: "all", category: "all", search: "" };
  var rows = [];

  // ---------- helpers ----------
  function el(tag, props) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else node.setAttribute(key, value === true ? "" : value);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }

  function categoryLabel(key) {
    return (CATEGORIES[key] && CATEGORIES[key].label) || key.replace(/_/g, " ");
  }

  function formatValue(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "number") {
      return Number.isInteger(value)
        ? value.toLocaleString("en-US")
        : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
    }
    return String(value);
  }

  function stage(trace, name) {
    var stages = (trace && trace.stages) || [];
    for (var i = 0; i < stages.length; i++) if (stages[i].name === name) return stages[i];
    return null;
  }

  function stopLayer(result) {
    if (result.decided_by && result.decided_by.name) return result.decided_by.name;
    return (result.trace && result.trace.refused_by && result.trace.refused_by.name) || null;
  }

  function layerText(name) {
    var layer = LAYERS[name];
    return layer ? "Layer " + layer.n + " · " + layer.label : name;
  }

  function findingText(item) {
    if (item === null || item === undefined) return "";
    if (typeof item !== "object") return String(item);
    return Object.keys(item)
      .filter(function (k) { return item[k] !== null && item[k] !== ""; })
      .map(function (k) { return item[k]; })
      .join(" — ");
  }

  // ---------- badges ----------
  function outcomeBadge(result) {
    if (result.actual === "answer") {
      return el("span", { class: "badge ok" }, "✓ Answered");
    }
    var layer = LAYERS[stopLayer(result)];
    return el("span", null,
      el("span", { class: "badge block" }, "⛔ Blocked"),
      el("span", { class: "sub-note", text: layer ? "at layer " + layer.n + ": " + layer.label : "" }),
      result.pass ? null : el("span", { class: "sub-note", text: "not as expected" })
    );
  }

  // Does the independent reviewer back the pipeline's outcome for this input?
  var ALIGN = {
    agree: { label: "✓ Agrees", tone: "ok" },
    partly: { label: "≈ Partly agrees", tone: "warn" },
    disagree: { label: "✗ Disagrees", tone: "block" },
    none: { label: "– No verdict", tone: "neutral" }
  };

  function alignment(result) {
    var review = result.review;
    if (!review) return { key: "none", why: "not reviewed" };
    var v = review.verdict;
    if (v === "cannot_tell") return { key: "none", why: "couldn't judge this one" };
    var key = v === "correct" ? "agree" : v === "partially_correct" ? "partly" : "disagree";

    // Blocked raw SQL is reviewed as SQL: calling that SQL wrong backs the block.
    if (review.review_type === "sql_only" && result.actual === "refuse") {
      if (v === "incorrect") return { key: "agree", why: "the pasted SQL was bad, so blocking was right" };
      if (v === "correct") return { key: "disagree", why: "the pasted SQL looked fine" };
      return { key: "partly", why: "the pasted SQL was partly fine" };
    }
    if (review.review_type === "refusal") {
      return { key: key, why: { agree: "refusing was right", partly: "refusal partly justified", disagree: "it should have answered" }[key] };
    }
    return { key: key, why: { agree: "the answer is right", partly: "the answer is partly right", disagree: "the answer is wrong" }[key] };
  }

  function reviewBadge(result) {
    var a = alignment(result);
    return el("span", null,
      el("span", { class: "badge " + ALIGN[a.key].tone, text: ALIGN[a.key].label }),
      el("span", { class: "sub-note", text: a.why })
    );
  }

  // ---------- header, diagram, summary ----------
  function renderStats(results) {
    var answered = results.filter(function (r) { return r.actual === "answer"; }).length;
    var passed = results.filter(function (r) { return r.pass; }).length;
    document.getElementById("stat-total").textContent = results.length;
    document.getElementById("stat-answered").textContent = answered;
    document.getElementById("stat-blocked").textContent = results.length - answered;
    document.getElementById("stat-pass").textContent = passed + "/" + results.length;
  }

  function renderRunNote(data) {
    if (!data.generated_at) return;
    var date = new Date(data.generated_at);
    if (isNaN(date.getTime())) return;
    var note = document.getElementById("run-note");
    append(note, " Recorded on " + date.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }) + ".");
  }

  function renderDiagram(results) {
    var counts = {};
    results.forEach(function (r) {
      var name = r.actual === "answer" ? null : stopLayer(r);
      if (name) counts[name] = (counts[name] || 0) + 1;
    });
    document.querySelectorAll(".flow [data-layer]").forEach(function (node) {
      var n = counts[node.getAttribute("data-layer")] || 0;
      var slot = node.querySelector(".stopped");
      slot.textContent = "Stopped " + n + " of the " + results.length + " test inputs";
      slot.className = "stopped " + (n ? "has" : "none");
    });
  }

  function renderCategories(results) {
    var grid = document.getElementById("cat-grid");
    var select = document.getElementById("cat-filter");
    var order = [];
    var stats = {};
    results.forEach(function (r) {
      if (!stats[r.category]) { stats[r.category] = { total: 0, answered: 0, pass: 0 }; order.push(r.category); }
      var s = stats[r.category];
      s.total++;
      if (r.actual === "answer") s.answered++;
      if (r.pass) s.pass++;
    });

    grid.textContent = "";
    order.forEach(function (key) {
      var s = stats[key];
      var blocked = s.total - s.answered;
      var bar = el("span", { class: "bar", "aria-hidden": "true" });
      if (s.answered) bar.appendChild(el("span", { class: "a", style: "flex:" + s.answered }));
      if (blocked) bar.appendChild(el("span", { class: "b", style: "flex:" + blocked }));

      var parts = [];
      if (s.answered) parts.push(s.answered + " answered");
      if (blocked) parts.push(blocked + " blocked");

      var card = el("button", { type: "button", class: "cat", "data-cat": key, "aria-pressed": "false" },
        el("span", { class: "cat-top" },
          el("span", { class: "cat-name", text: categoryLabel(key) }),
          el("span", { class: "cat-n", text: s.total + (s.total > 1 ? " inputs" : " input") })
        ),
        el("span", { class: "cat-desc", text: (CATEGORIES[key] && CATEGORIES[key].desc) || "" }),
        bar,
        el("span", { class: "cat-foot" },
          el("span", { text: parts.join(" · ") }),
          s.pass === s.total
            ? el("span", { class: "ok", text: "✓ all as expected" })
            : el("span", { class: "fail", text: (s.total - s.pass) + " unexpected" })
        )
      );
      card.addEventListener("click", function () {
        setCategory(state.category === key ? "all" : key);
      });
      grid.appendChild(card);
      select.appendChild(el("option", { value: key, text: categoryLabel(key) + " (" + s.total + ")" }));
    });
  }

  // ---------- detail panel ----------
  function section(title) {
    var box = el("div", null, el("h4", { text: title }));
    for (var i = 1; i < arguments.length; i++) append(box, arguments[i]);
    return box;
  }

  function inputSection(result) {
    var trace = result.trace || {};
    var screen = stage(trace, "input_screen");
    var flags = (screen && screen.details && screen.details.flags) || [];
    var isRaw = trace.input_type === "raw_sql";
    return section("The input",
      el("pre", { class: "full-input", text: trace.input || result.question }),
      el("p", { class: "meta-line" },
        isRaw ? "Treated as SQL pasted directly (skips the AI). " : "Treated as a plain-English question. ",
        "Expected: ", el("strong", { text: result.expect === "answer" ? "an answer" : "a refusal" }),
        ". Got: ", el("strong", { text: result.actual === "answer" ? "an answer" : "a refusal" }),
        result.pass ? " ✓" : " ✗",
        flags.length ? el("span", null, " · Warning signs noticed: ", flags.map(function (f) { return el("span", { class: "tag", text: f }); })) : null
      )
    );
  }

  function journeySection(trace) {
    var list = el("ol", { class: "journey" });
    Object.keys(LAYERS).forEach(function (name) {
      var s = stage(trace, name);
      var status = s ? s.status : "skipped";
      list.appendChild(el("li", { class: status },
        el("span", { class: "st", text: STATUS[status] || status }),
        LAYERS[name].n + " · " + LAYERS[name].label
      ));
    });
    return section("Its path through the five layers", list);
  }

  function sqlSection(result) {
    var trace = result.trace || {};
    var gen = stage(trace, "generation");
    var genDetails = (gen && gen.details) || {};
    var stop = stopLayer(result);
    var parts = [];

    if (trace.input_type === "raw_sql") {
      parts.push(el("p", { class: "sql-label", text: "SQL as submitted" }), el("pre", { class: "sql", text: trace.input }));
    } else if (genDetails.proposed_sql) {
      parts.push(el("p", { class: "sql-label", text: "SQL proposed by the AI" }), el("pre", { class: "sql", text: genDetails.proposed_sql }));
    } else if (genDetails.refusal_reason) {
      parts.push(el("p", { class: "reason" }, el("strong", { text: "The AI declined to write SQL: " }), genDetails.refusal_reason));
    } else if (stop === "input_screen") {
      parts.push(el("p", { class: "reason" }, el("strong", { text: "Stopped before the AI saw it: " }), trace.reason || ""));
    }

    if (trace.normalized_sql && result.actual === "answer") {
      parts.push(
        el("p", { class: "sql-label", text: "SQL that actually ran (rebuilt by the validator, row limit enforced)" }),
        el("pre", { class: "sql", text: trace.normalized_sql })
      );
    }
    if (!parts.length) parts.push(el("p", { class: "muted", text: "No SQL was produced." }));
    return section("The SQL", parts);
  }

  function validatorSection(result) {
    var trace = result.trace || {};
    var v = stage(trace, "validator");
    var checks = (v && v.details && v.details.checks) || [];
    if (!v || v.status === "skipped" || !checks.length) {
      var stop = stopLayer(result);
      return section("Guardrail verdict (sqlglot validator)",
        el("p", { class: "muted", text: "Not reached: the input was already stopped at " + (stop ? layerText(stop).toLowerCase() : "an earlier layer") + ", so there was no SQL to check." })
      );
    }
    var list = el("ul", { class: "checks" });
    checks.forEach(function (c) {
      list.appendChild(el("li", { class: c.passed ? "pass" : "fail" },
        el("span", { class: "mark", "aria-label": c.passed ? "passed" : "failed", text: c.passed ? "✓" : "✗" }),
        el("span", null,
          el("span", { text: CHECKS[c.name] || c.name }),
          el("span", { class: "cd", text: c.detail || "" })
        )
      ));
    });
    var verdict = v.status === "pass"
      ? el("p", { class: "reason ok" }, el("strong", { text: "Accepted. " }), "Every check passed, so the query was allowed to run.")
      : el("p", { class: "reason" }, el("strong", { text: "Rejected. " }), (v.details && v.details.reason) || "",
          " Checking stops at the first failure.");
    return section("Guardrail verdict (sqlglot validator)", verdict, el("div", { style: "height:8px" }), list);
  }

  function resultSection(result) {
    var res = result.trace && result.trace.result;
    if (!res || !res.columns) {
      return section("The result", el("p", { class: "muted", text: "Nothing ran, so no data was returned." }));
    }
    var head = el("tr");
    res.columns.forEach(function (c) { head.appendChild(el("th", { scope: "col", text: c })); });
    var body = el("tbody");
    (res.rows || []).forEach(function (row) {
      var tr = el("tr");
      row.forEach(function (cell) {
        tr.appendChild(el("td", { class: typeof cell === "number" ? "num" : null, text: formatValue(cell) }));
      });
      body.appendChild(tr);
    });
    var shown = (res.rows || []).length;
    var note = shown + (shown === 1 ? " row" : " rows") + (res.truncated ? " shown (preview of a longer result)" : "");
    var oc = stage(result.trace, "output_check");
    var ocd = (oc && oc.details) || {};
    var extras = [];
    if (ocd.dropped_columns && ocd.dropped_columns.length) extras.push("restricted columns removed: " + ocd.dropped_columns.join(", "));
    if (ocd.masked_cells) extras.push(ocd.masked_cells + " cells masked");
    return section("The result",
      el("div", { class: "data-wrap" }, el("table", { class: "data" }, el("thead", null, head), body)),
      el("p", { class: "meta-line", text: note + (extras.length ? " · " + extras.join(" · ") : "") })
    );
  }

  function reviewSection(result) {
    var review = result.review;
    if (!review) return section("The review", el("p", { class: "muted", text: "No review recorded." }));
    var v = VERDICTS[review.verdict] || { label: review.verdict, tone: "neutral" };
    var a = alignment(result);
    var f = review.findings || {};
    var items = [];
    (f.logic || []).forEach(function (x) { items.push(["Logic", findingText(x)]); });
    (f.data || []).forEach(function (x) { items.push(["Data", findingText(x)]); });
    (f.assumptions || []).forEach(function (x) {
      var text = typeof x === "object" ? x.assumption : x;
      var ok = typeof x === "object" && x.reasonable === false ? " (questionable)" : "";
      items.push(["Assumption", text + ok]);
    });
    (f.unverifiable || []).forEach(function (x) { items.push(["Couldn't verify", findingText(x)]); });

    var list = items.length ? el("ul", { class: "findings" }, items.map(function (it) {
      return el("li", null, el("span", { class: "fk", text: it[0] + ": " }), it[1]);
    })) : null;

    return section("The review (independent AI, no database access)",
      el("div", { class: "review-head" },
        el("span", { class: "badge " + ALIGN[a.key].tone, text: ALIGN[a.key].label + " with the outcome" }),
        el("span", { class: "muted", text: a.why + " · raw verdict: " + v.label + " " + (REVIEW_OF[review.review_type] || "") +
          (review.confidence ? " · " + review.confidence + " confidence" : "") }),
        review.injection_attempt ? el("span", { class: "badge warn", text: "Manipulation attempt spotted" }) : null
      ),
      el("p", { text: review.summary || "" }),
      list,
      review.corrected_sql ? [el("p", { class: "sql-label", text: "Reviewer's suggested SQL" }), el("pre", { class: "sql", text: review.corrected_sql })] : null
    );
  }

  function buildPanel(result) {
    return el("div", { class: "panel" },
      inputSection(result),
      journeySection(result.trace || {}),
      sqlSection(result),
      validatorSection(result),
      resultSection(result),
      reviewSection(result)
    );
  }

  // ---------- table ----------
  function renderTable(results) {
    var tbody = document.getElementById("rows");
    tbody.textContent = "";
    results.forEach(function (r, i) {
      var detailId = "detail-" + i;
      var isRaw = r.trace && r.trace.input_type === "raw_sql";
      var btn = el("button", { type: "button", class: "q-btn", "aria-expanded": "false", "aria-controls": detailId },
        el("span", { class: "chev", "aria-hidden": "true", text: "▶" }),
        el("span", { class: "q-text" + (isRaw ? " sql" : ""), text: r.question })
      );
      var main = el("tr", { class: "main" },
        el("td", null, btn),
        el("td", { class: "col-cat" }, el("span", { class: "badge cat-badge", text: categoryLabel(r.category) })),
        el("td", null, outcomeBadge(r)),
        el("td", { class: "col-review" }, reviewBadge(r))
      );
      var cell = el("td", { colspan: "4" });
      var detail = el("tr", { class: "detail", id: detailId, hidden: true }, cell);

      function setOpen(open) {
        if (open && !cell.firstChild) cell.appendChild(buildPanel(r));
        btn.setAttribute("aria-expanded", String(open));
        main.classList.toggle("open", open);
        detail.hidden = !open || main.hidden;
      }

      btn.addEventListener("click", function () {
        setOpen(btn.getAttribute("aria-expanded") !== "true");
      });

      var haystack = [r.question, r.category, categoryLabel(r.category), r.trace && r.trace.reason,
        r.trace && r.trace.generated_sql, r.review && r.review.summary].join(" ").toLowerCase();
      rows.push({ result: r, main: main, detail: detail, btn: btn, setOpen: setOpen, haystack: haystack });
      tbody.appendChild(main);
      tbody.appendChild(detail);
    });
    tbody.appendChild(el("tr", { id: "empty-row", hidden: true },
      el("td", { colspan: "4", class: "empty", text: "No inputs match these filters." })));
    applyFilters();
  }

  function applyFilters() {
    var shown = 0;
    var query = state.search.trim().toLowerCase();
    rows.forEach(function (row) {
      var r = row.result;
      var visible = (state.outcome === "all" || r.actual === state.outcome) &&
        (state.category === "all" || r.category === state.category) &&
        (!query || row.haystack.indexOf(query) !== -1);
      row.main.hidden = !visible;
      row.detail.hidden = !visible || row.btn.getAttribute("aria-expanded") !== "true";
      if (visible) shown++;
    });
    document.getElementById("empty-row").hidden = shown !== 0;
    document.getElementById("count").textContent = "Showing " + shown + " of " + rows.length + " inputs";
  }

  function setCategory(key) {
    state.category = key;
    document.getElementById("cat-filter").value = key;
    document.querySelectorAll(".cat").forEach(function (c) {
      c.setAttribute("aria-pressed", String(c.getAttribute("data-cat") === key));
    });
    applyFilters();
  }

  function wireFilters() {
    document.querySelectorAll("#outcome-filter button").forEach(function (b) {
      b.addEventListener("click", function () {
        state.outcome = b.getAttribute("data-outcome");
        document.querySelectorAll("#outcome-filter button").forEach(function (o) {
          o.setAttribute("aria-pressed", String(o === b));
        });
        applyFilters();
      });
    });
    document.getElementById("cat-filter").addEventListener("change", function (e) { setCategory(e.target.value); });
    document.getElementById("search").addEventListener("input", function (e) {
      state.search = e.target.value;
      applyFilters();
    });
    // Expand only the rows the current filters show; collapse every row.
    document.getElementById("expand-all").addEventListener("click", function () {
      rows.forEach(function (row) { if (!row.main.hidden) row.setOpen(true); });
    });
    document.getElementById("collapse-all").addEventListener("click", function () {
      rows.forEach(function (row) { row.setOpen(false); });
    });
  }

  // ---------- boot ----------
  function init(data) {
    var results = (data && data.results) || [];
    renderStats(results);
    renderRunNote(data);
    renderDiagram(results);
    renderCategories(results);
    renderTable(results);
    wireFilters();
  }

  function fail() {
    var tbody = document.getElementById("rows");
    tbody.textContent = "";
    tbody.appendChild(el("tr", null, el("td", { colspan: "4", class: "empty" },
      "Couldn't load results.json. If you opened this file directly from disk, serve the folder instead: ",
      el("code", { text: "python -m http.server -d showcase" })
    )));
  }

  fetch("results.json")
    .then(function (res) { if (!res.ok) throw new Error(res.status); return res.json(); })
    .then(init)
    .catch(fail);
})();
