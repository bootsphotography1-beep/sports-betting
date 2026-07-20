(() => {
  const state = {
    payload: null,
    activeSport: "ALL",
  };

  const els = {
    status: document.getElementById("status"),
    tabs: document.getElementById("sport-tabs"),
    panels: document.getElementById("sport-panels"),
    meta: document.getElementById("meta-strip"),
    metaOpps: document.getElementById("meta-opps"),
    metaMis: document.getElementById("meta-mis"),
    metaSharp: document.getElementById("meta-sharp"),
    metaSources: document.getElementById("meta-sources"),
    method: document.getElementById("method"),
    methodTitle: document.getElementById("method-title"),
    methodSub: document.getElementById("method-sub"),
    methodSteps: document.getElementById("method-steps"),
    lineups: document.getElementById("lineups"),
    lineupList: document.getElementById("lineup-list"),
    toast: document.getElementById("toast"),
    entry: document.getElementById("entry"),
    fullGame: document.getElementById("full-game"),
    mispricedOnly: document.getElementById("mispriced-only"),
  };

  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.hidden = false;
    els.toast.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      els.toast.classList.remove("show");
    }, 1800);
  }

  async function copyText(text, label) {
    if (!text) {
      toast("Nothing to copy");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${label}`);
    } catch {
      // Fallback for older browsers / insecure context
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      toast(`Copied ${label}`);
    }
  }

  function queryParams(refresh = false) {
    const params = new URLSearchParams({
      entry: els.entry.value,
      full_game_only: String(els.fullGame.checked),
      mispriced_only: String(els.mispricedOnly.checked),
      refresh: String(refresh),
      n_entries: "4",
    });
    return params;
  }

  async function load(refresh = false) {
    els.status.textContent = refresh ? "Refreshing live slate…" : "Loading…";
    try {
      const res = await fetch(`/api/opportunities?${queryParams(refresh)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state.payload = await res.json();
      render();
      const t = state.payload.totals || {};
      els.status.textContent = `Updated ${new Date(state.payload.fetched_at).toLocaleString()} · ${t.opportunities || 0} picks`;
    } catch (err) {
      els.status.textContent = `Failed to load: ${err.message}`;
      els.panels.innerHTML = `<p class="empty">Could not load opportunities. Is the API running?</p>`;
    }
  }

  function render() {
    const data = state.payload;
    if (!data) return;

    const totals = data.totals || {};
    els.meta.hidden = false;
    els.metaOpps.textContent = totals.opportunities ?? 0;
    els.metaMis.textContent = totals.mispriced ?? 0;
    els.metaSharp.textContent = data.sharp_meta?.count ?? 0;
    const sources = (data.sharp_meta?.sources || []).join(", ") || "manual CSV / env keys";
    const fantasy = Object.entries(data.fantasy_meta?.sources || {})
      .map(([k, v]) => `${k}: ${v}`)
      .join(" · ");
    els.metaSources.textContent = fantasy
      ? `Fantasy ${fantasy} · Sharp ${sources}`
      : `Sharp ${sources}`;

    renderSafetyBanner(data.safety_status);
    renderMethod(data.methodology);
    renderTabs(data.sports || []);
    renderPanels(data.sports || []);
    renderLineups(data.lineups || []);
  }

  function renderSafetyBanner(status) {
    const existing = document.getElementById("safety-banner");
    if (existing) existing.remove();
    if (!status || !status.is_research_mode) return;
    const banner = document.createElement("div");
    banner.id = "safety-banner";
    banner.className = "safety-banner";
    banner.setAttribute("role", "alert");

    // Use textContent for all text segments (numeric fields are defensive)
    const strong = document.createElement("strong");
    strong.textContent = "\u26A0\uFE0F UNVERIFIED RESEARCH MODE";

    const seg1 = document.createElement("span");
    seg1.textContent = " \u2014 EV/$ and win% are unverified research estimates. ";

    const seg2 = document.createElement("span");
    seg2.textContent = "Payout model unverified \u00B7 ";
    const settledCount = document.createElement("span");
    settledCount.textContent = String(status.settled_legs_count ?? 0);
    const seg2b = document.createElement("span");
    seg2b.textContent = "/" + String(status.min_settled_legs_required ?? 50);
    const seg2c = document.createElement("span");
    seg2c.textContent = " settled legs. ";

    const seg3 = document.createElement("span");
    seg3.textContent = "Do not treat as actionable +EV. See ";

    const link = document.createElement("a");
    link.setAttribute("href", "/HONEST_STATUS.md");
    link.textContent = "HONEST_STATUS.md";

    const seg4 = document.createElement("span");
    seg4.textContent = " for full safety case.";

    banner.appendChild(strong);
    banner.appendChild(seg1);
    banner.appendChild(seg2);
    seg2.appendChild(settledCount);
    seg2.appendChild(seg2b);
    seg2.appendChild(seg2c);
    banner.appendChild(seg3);
    banner.appendChild(link);
    banner.appendChild(seg4);

    document.querySelector(".hero")?.appendChild(banner);
  }

  function renderMethod(method) {
    if (!method || !els.method) return;
    els.method.hidden = false;
    if (method.title) els.methodTitle.textContent = method.title;
    const be = method.break_even != null ? (method.break_even * 100).toFixed(1) : null;
    els.methodSub.textContent = be
      ? `Entry ${method.entry_type || ""} · break-even ${be}% per leg. Expand any pick for the math.`
      : "Each prop below includes the math edge and why it made the board.";
    els.methodSteps.innerHTML = "";
    for (const step of method.steps || []) {
      const li = document.createElement("li");
      li.textContent = step;
      els.methodSteps.appendChild(li);
    }
  }

  function renderTabs(sports) {
    const total = sports.reduce((n, s) => n + s.count, 0);
    const tabs = [
      { sport: "ALL", count: total, mispriced_count: sports.reduce((n, s) => n + s.mispriced_count, 0) },
      ...sports,
    ];

    els.tabs.innerHTML = "";
    for (const s of tabs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sport-tab" + (state.activeSport === s.sport ? " active" : "");
      btn.innerHTML = `${s.sport}<span class="count">${s.count}</span>`;
      btn.addEventListener("click", () => {
        state.activeSport = s.sport;
        renderTabs(sports);
        renderPanels(sports);
      });
      els.tabs.appendChild(btn);
    }
  }

  function renderPanels(sports) {
    els.panels.innerHTML = "";
    const visible = state.activeSport === "ALL"
      ? sports
      : sports.filter((s) => s.sport === state.activeSport);

    if (!visible.length) {
      els.panels.innerHTML = `<p class="empty">No opportunities for this filter. Try refreshing or lowering thresholds.</p>`;
      return;
    }

    for (const block of visible) {
      const panel = document.createElement("article");
      panel.className = "sport-panel";
      panel.dataset.sport = block.sport;

      const head = document.createElement("div");
      head.className = "panel-head";
      head.innerHTML = `
        <div>
          <h2>${block.sport}</h2>
          <p>${block.count} picks · ${block.mispriced_count} mispriced vs sharp</p>
        </div>
      `;

      const copyGroup = document.createElement("div");
      copyGroup.className = "copy-group";
      for (const [platform, label] of [
        ["underdog", "Underdog"],
        ["prizepicks", "PrizePicks"],
        ["sleeper", "Sleeper"],
      ]) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn ghost small";
        b.textContent = `Copy ${label}`;
        b.addEventListener("click", () => {
          copyText(block.copy?.[platform] || "", `${block.sport} · ${label}`);
        });
        copyGroup.appendChild(b);
      }
      head.appendChild(copyGroup);
      panel.appendChild(head);

      const table = document.createElement("table");
      table.className = "pick-table";
      table.innerHTML = `
        <thead>
          <tr>
            <th>Player</th>
            <th>Pick</th>
            <th>UD%</th>
            <th>Sharp%</th>
            <th>Δpp</th>
            <th>Why this pick</th>
            <th>Copy</th>
          </tr>
        </thead>
      `;
      const tbody = document.createElement("tbody");

      for (const opp of block.opportunities || []) {
        const tr = document.createElement("tr");
        const mis = opp.is_mispriced;
        const sharpPct = opp.sharp_true_prob != null
          ? `${(opp.sharp_true_prob * 100).toFixed(1)}%`
          : "—";
        const delta = opp.mispricing_edge_pp != null
          ? `${opp.mispricing_edge_pp >= 0 ? "+" : ""}${opp.mispricing_edge_pp.toFixed(1)}`
          : "—";
        const book = opp.sharp_book ? `<span class="badge${mis ? " hot" : ""}">${opp.sharp_book}</span>` : "";
        const reason = opp.reason || {};
        const reasonId = `reason-${block.sport}-${Math.random().toString(36).slice(2, 9)}`;

        tr.innerHTML = `
          <td data-label="Player">
            <div class="player">${escapeHtml(opp.player_name)}</div>
            <span class="match">${escapeHtml(opp.match_title || "")}</span>
          </td>
          <td data-label="Pick">
            <div class="pick-main">${escapeHtml(opp.side_label)} ${opp.line_value} ${escapeHtml(opp.stat_label)}</div>
            ${book}
          </td>
          <td data-label="UD%">${(opp.ud_true_prob * 100).toFixed(1)}%</td>
          <td data-label="Sharp%">${sharpPct}</td>
          <td data-label="Δpp">${delta}</td>
          <td data-label="Why" class="reason-cell"></td>
          <td data-label="Copy"></td>
        `;

        const reasonTd = tr.querySelector(".reason-cell");
        const headline = document.createElement("p");
        headline.className = "reason-headline";
        headline.textContent = reason.headline || "No-vig edge";
        reasonTd.appendChild(headline);

        const summary = document.createElement("p");
        summary.className = "reason-summary";
        summary.textContent = reason.summary || "";
        reasonTd.appendChild(summary);

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "reason-toggle";
        toggle.textContent = "Show math & reasoning";
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-controls", reasonId);

        const details = document.createElement("div");
        details.className = "reason-details";
        details.id = reasonId;
        details.hidden = true;

        const why = document.createElement("p");
        why.className = "why-shown";
        why.textContent = reason.why_shown || "";
        details.appendChild(why);

        const ul = document.createElement("ul");
        for (const b of reason.bullets || []) {
          const li = document.createElement("li");
          li.textContent = b;
          ul.appendChild(li);
        }
        details.appendChild(ul);

        if (reason.math && reason.math.length) {
          const math = document.createElement("pre");
          math.className = "reason-math";
          math.textContent = reason.math.join("\n");
          details.appendChild(math);
        }

        toggle.addEventListener("click", () => {
          const open = details.hidden;
          details.hidden = !open;
          toggle.setAttribute("aria-expanded", String(open));
          toggle.textContent = open ? "Hide math & reasoning" : "Show math & reasoning";
        });

        reasonTd.appendChild(toggle);
        reasonTd.appendChild(details);

        const actions = document.createElement("div");
        actions.className = "row-actions";
        for (const [key, label] of [
          ["underdog", "UD"],
          ["prizepicks", "PP"],
          ["sleeper", "SL"],
        ]) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "btn ghost small";
          b.textContent = label;
          b.title = `Copy for ${label}`;
          b.addEventListener("click", () => {
            copyText(opp.copy?.[key] || "", label);
          });
          actions.appendChild(b);
        }
        tr.lastElementChild.appendChild(actions);
        tbody.appendChild(tr);
      }

      table.appendChild(tbody);
      panel.appendChild(table);
      els.panels.appendChild(panel);
    }
  }

  function renderLineups(lineups) {
    if (!lineups.length) {
      els.lineups.hidden = true;
      return;
    }
    els.lineups.hidden = false;
    els.lineupList.innerHTML = "";

    for (const lu of lineups) {
      const wrap = document.createElement("div");
      wrap.className = "lineup";
      const head = document.createElement("div");
      head.className = "lineup-head";
      head.innerHTML = `<h3>Entry #${lu.entry}</h3><span>${(lu.avg_true_prob * 100).toFixed(1)}% avg true</span>`;

      const copies = document.createElement("div");
      copies.className = "copy-group";
      for (const [platform, label] of [
        ["underdog", "Underdog"],
        ["prizepicks", "PrizePicks"],
        ["sleeper", "Sleeper"],
      ]) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn ghost small";
        b.textContent = `Copy ${label}`;
        b.addEventListener("click", () => copyText(lu.copy?.[platform] || "", `Entry #${lu.entry} · ${label}`));
        copies.appendChild(b);
      }
      head.appendChild(copies);
      wrap.appendChild(head);

      const ol = document.createElement("ol");
      for (const opp of lu.opportunities || []) {
        const li = document.createElement("li");
        const main = document.createElement("div");
        main.textContent = `${opp.sport_id} · ${opp.copy?.underdog || opp.player_name}`;
        li.appendChild(main);
        if (opp.reason?.headline) {
          const why = document.createElement("div");
          why.className = "reason-summary";
          why.textContent = opp.reason.headline;
          li.appendChild(why);
        }
        ol.appendChild(li);
      }
      wrap.appendChild(ol);
      els.lineupList.appendChild(wrap);
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  document.getElementById("btn-refresh").addEventListener("click", () => load(true));
  document.getElementById("btn-copy-all-ud").addEventListener("click", () => {
    copyText(state.payload?.copy_all?.underdog || "", "all Underdog");
  });
  document.getElementById("btn-copy-all-pp").addEventListener("click", () => {
    copyText(state.payload?.copy_all?.prizepicks || "", "all PrizePicks");
  });
  document.getElementById("btn-copy-all-sl").addEventListener("click", () => {
    copyText(state.payload?.copy_all?.sleeper || "", "all Sleeper");
  });

  els.entry.addEventListener("change", () => load(false));
  els.fullGame.addEventListener("change", () => load(false));
  els.mispricedOnly.addEventListener("change", () => load(false));

  load(true);
})();
