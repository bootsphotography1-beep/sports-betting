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

    renderTabs(data.sports || []);
    renderPanels(data.sports || []);
    renderLineups(data.lineups || []);
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
          <td data-label="Copy"></td>
        `;

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
        li.textContent = `${opp.sport_id} · ${opp.copy?.underdog || opp.player_name}`;
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
