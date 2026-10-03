(function () {
  "use strict";

  var TZ = "America/Chicago";
  var TEXTURE_CDN = "https://textures.minecraft.net/texture/";
  var EDITIONS = { java: "Java Edition", bedrock: "Bedrock Edition" };

  var state = { data: null, status: null, edition: "java", sourceOrder: [] };

  // ---------- time helpers ----------

  var fmtDateTime = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, weekday: "short", month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit", timeZoneName: "short"
  });
  var fmtDate = new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC", weekday: "short", month: "short", day: "numeric", year: "numeric"
  });
  var fmtShortDate = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, month: "short", day: "numeric", year: "numeric"
  });

  // Date-only values ("2026-10-31") are calendar days, not instants.
  // They run from 00:00 to 23:59 US Central time (CST or CDT, whichever applies).
  var fmtHour = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", hourCycle: "h23" });
  function dayStart(d) {
    var guess = Date.parse(d + "T06:00:00Z"); // midnight CST
    var hour = +fmtHour.format(new Date(guess)); // 1 during CDT
    return guess - hour * 3600000;
  }
  function dayEnd(d) { return dayStart(d) + 86400000 - 1; }

  function windowStart(w) {
    if (!w.start) return -Infinity;
    return w.dateOnly ? dayStart(w.start) : Date.parse(w.start);
  }
  function windowEnd(w) {
    if (!w.end) return Infinity;
    if (w.dateOnly) return w.exclusiveEnd ? dayStart(w.end) : dayEnd(w.end);
    return Date.parse(w.end);
  }

  function showTime(value, dateOnly) {
    if (!value) return "—";
    if (dateOnly) return fmtDate.format(new Date(value + "T12:00:00Z"));
    return fmtDateTime.format(new Date(value));
  }

  function addMonths(ms, n) {
    var d = new Date(ms);
    d.setUTCMonth(d.getUTCMonth() + n);
    return d.getTime();
  }

  function countdown(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + "d " + h + "h";
    if (h > 0) return h + "h " + m + "m";
    return m + "m";
  }

  // ---------- status ----------

  function locStatus(loc, now) {
    if (loc.tba) return "upcoming";
    if (now < dayStart(loc.start)) return "upcoming";
    if (now > dayEnd(loc.end)) return "closed";
    return "open";
  }

  function capeStatus(cape, now) {
    if (cape.always) return { code: "always" };

    var earn = [], redeem = [];
    (cape.windows || []).forEach(function (w) {
      (w.role === "redeem" ? redeem : earn).push(w);
    });
    (cape.locations || []).forEach(function (l) {
      if (l.tba) earn.push({ tba: true });
      else earn.push({ start: l.start, end: l.end, dateOnly: true });
    });

    var openUntil = null, nextStart = null, closedAt = -Infinity, hasTba = false;
    earn.forEach(function (w) {
      if (w.tba) { hasTba = true; return; }
      var s = windowStart(w), e = windowEnd(w);
      if (s <= now && now < e) openUntil = Math.max(openUntil || 0, e);
      else if (s > now) nextStart = nextStart === null ? s : Math.min(nextStart, s);
      closedAt = Math.max(closedAt, e);
    });

    if (openUntil !== null) return { code: "live", until: openUntil, closedAt: closedAt };
    if (nextStart !== null || hasTba) return { code: "upcoming", from: nextStart };

    var redeemUntil = null;
    redeem.forEach(function (w) {
      var e = windowEnd(w);
      if (e > now) redeemUntil = Math.max(redeemUntil || 0, e);
    });
    return { code: "ended", closedAt: closedAt, redeemUntil: redeemUntil };
  }

  function isHidden(cape, st, now) {
    if (st.code !== "ended") return false;
    var months = (state.data && state.data.hideAfterMonths) || 2;
    return now > addMonths(st.closedAt, months);
  }

  // ---------- rendering helpers ----------

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function citeNum(id) {
    var i = state.sourceOrder.indexOf(id);
    if (i === -1) { state.sourceOrder.push(id); i = state.sourceOrder.length - 1; }
    return i + 1;
  }

  function cites(ids) {
    return (ids || []).map(function (id) {
      var n = citeNum(id);
      return '<a href="#src-' + n + '" title="' + esc(state.data.sources[id].title) + '">[' + n + "]</a>";
    }).join(" ");
  }

  function artStyle(texture) {
    var local = "textures/" + texture + ".png";
    var remote = TEXTURE_CDN + texture;
    return "background-image:url('" + local + "'),url('" + remote + "')";
  }

  function badge(st, now) {
    switch (st.code) {
      case "always": return '<span class="badge always">Always available</span>';
      case "live": return '<span class="badge live">Available now</span>';
      case "upcoming": return '<span class="badge soon">Upcoming</span>';
      default:
        return st.redeemUntil
          ? '<span class="badge ended">Ended · code redemption open</span>'
          : '<span class="badge ended">No longer obtainable</span>';
    }
  }

  function editionBadge(cape) {
    var both = cape.editions.indexOf("java") > -1 && cape.editions.indexOf("bedrock") > -1;
    return '<span class="badge">' + (both ? "Java &amp; Bedrock" : esc(EDITIONS[cape.editions[0]])) + "</span>";
  }

  function statusLine(st, now, cape) {
    if (st.code === "live" && cape && cape.locations) {
      var open = cape.locations.filter(function (l) { return locStatus(l, now) === "open"; }).length;
      return '<p><span class="countdown">Open now in ' + open + " " + (open === 1 ? "city" : "cities") +
        '</span> <span class="muted">· see locations below</span></p>';
    }
    if (st.code === "live" && isFinite(st.until)) {
      return '<p><span class="countdown" data-until="' + st.until + '">Ends in ' + countdown(st.until - now) +
        "</span> <span class=\"muted\">(" + esc(fmtDateTime.format(new Date(st.until))) + ")</span></p>";
    }
    if (st.code === "upcoming" && st.from && cape && cape.locations) {
      var next = cape.locations.filter(function (l) { return locStatus(l, now) === "upcoming" && !l.tba; })
        .sort(function (a, b) { return dayStart(a.start) - dayStart(b.start); })[0];
      if (next) return '<p class="muted">Opens ' + esc(showTime(next.start, true)) + " in " + esc(next.city) + " (local date)</p>";
    }
    if (st.code === "upcoming" && st.from) {
      return '<p class="muted">Starts ' + esc(fmtDateTime.format(new Date(st.from))) + "</p>";
    }
    if (st.code === "ended") {
      var s = '<p class="muted">Closed ' + esc(fmtShortDate.format(new Date(st.closedAt)));
      if (st.redeemUntil) s += " · already-earned codes can be redeemed until " + esc(fmtShortDate.format(new Date(st.redeemUntil)));
      return s + "</p>";
    }
    return "";
  }

  function windowsTable(cape) {
    if (!cape.windows || !cape.windows.length) return "";
    var rows = cape.windows.map(function (w) {
      var when;
      if (w.role === "redeem") when = "by " + showTime(w.end, w.dateOnly);
      else if (w.exclusiveEnd && w.dateOnly && !w.start) when = "until " + showTime(w.end, true);
      else if (w.exclusiveEnd && w.dateOnly) when = showTime(w.start, true) + " → before " + showTime(w.end, true);
      else when = showTime(w.start, w.dateOnly) + " → " + showTime(w.end, w.dateOnly);
      return "<tr><td>" + esc(w.label) + '</td><td class="when">' + esc(when) + "</td></tr>";
    }).join("");
    return '<h4>Key dates</h4><div class="scroll-x"><table class="tbl"><tbody>' + rows + "</tbody></table></div>";
  }

  function locationsTable(cape, now) {
    if (!cape.locations || !cape.locations.length) return "";
    var months = state.data.hideAfterMonths || 2;
    var order = { open: 0, upcoming: 1, closed: 2 };
    var locs = cape.locations
      .map(function (l) { return { l: l, s: locStatus(l, now) }; })
      .filter(function (x) { return !(x.s === "closed" && now > addMonths(dayEnd(x.l.end), months)); })
      .sort(function (a, b) {
        return order[a.s] - order[b.s] ||
          (a.l.tba ? 1 : b.l.tba ? -1 : (a.s === "upcoming" ? dayStart(a.l.start) - dayStart(b.l.start) : dayEnd(a.l.end) - dayEnd(b.l.end)));
      });
    var label = { open: '<span class="badge live">Open</span>', upcoming: '<span class="badge soon">Upcoming</span>', closed: '<span class="badge ended">Closed</span>' };
    var rows = locs.map(function (x) {
      var l = x.l;
      var dates = l.tba ? esc(l.tba) : esc(showTime(l.start, true)) + " → " + esc(showTime(l.end, true));
      var links = [];
      if (l.url) links.push('<a href="' + esc(l.url) + '" target="_blank" rel="noopener">Info</a>');
      if (l.tickets && x.s !== "closed") links.push('<a href="' + esc(l.tickets) + '" target="_blank" rel="noopener">Tickets</a>');
      return "<tr><td>" + label[x.s] + "</td><td><strong>" + esc(l.city) + '</strong><br><span class="note">' + esc(l.venue || "") +
        '</span></td><td class="when">' + dates + "</td><td>" + links.join(" · ") + "</td></tr>";
    }).join("");
    return '<h4>Locations</h4><div class="scroll-x"><table class="tbl loc"><thead><tr><th>Status</th><th>City</th><th>Dates (local)</th><th>Links</th></tr></thead><tbody>' +
      rows + "</tbody></table></div>";
  }

  function linksList(links) {
    if (!links || !links.length) return "";
    var kinds = { stream: "Watch", redeem: "Redeem", tickets: "Tickets", info: "Info" };
    return '<h4>Links</h4><ul class="chips">' + links.map(function (l) {
      return '<li><a class="' + esc(l.kind) + '" href="' + esc(l.url) + '" target="_blank" rel="noopener"><span class="k">' +
        esc(kinds[l.kind] || "Link") + "</span>" + esc(l.label) + "</a></li>";
    }).join("") + "</ul>";
  }

  function fullCard(cape, st, now, edition) {
    var name = cape.names[edition] || cape.names.java;
    var otherName = edition === "java" ? cape.names.bedrock : cape.names.java;
    var h = '<article class="card" id="' + esc(edition + "-" + cape.id) + '">';
    h += '<div class="art" role="img" aria-label="' + esc(name) + ' texture" style="' + artStyle(cape.texture) + '"></div><div>';
    h += '<div class="title-row"><h3>' + esc(name) + "</h3>" + badge(st, now) + editionBadge(cape) + "</div>";
    h += '<p class="cat">' + esc(cape.category);
    if (otherName && otherName !== name) h += ' · called “' + esc(otherName) + "” on " + (edition === "java" ? "Bedrock" : "Java");
    h += "</p>";
    h += statusLine(st, now, cape);
    (cape.alerts || []).forEach(function (a) { h += '<p class="alert-inline">⚠ ' + esc(a) + "</p>"; });
    h += "<h4>Appearance</h4><p>" + esc(cape.appearance) + "</p>";
    h += "<h4>How to get it</h4><ol>" + cape.steps.map(function (s) { return "<li>" + s + "</li>"; }).join("") + "</ol>";
    if (cape.editionNotes && cape.editionNotes[edition]) {
      h += '<p class="note" style="margin-top:8px"><strong>' + esc(EDITIONS[edition]) + ":</strong> " + esc(cape.editionNotes[edition]) + "</p>";
    }
    h += windowsTable(cape);
    h += locationsTable(cape, now);
    if (cape.notes && cape.notes.length) {
      h += "<h4>Good to know</h4><ul>" + cape.notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul>";
    }
    h += linksList(cape.links);
    h += '<p class="cite">Sources: ' + cites(cape.sources) + "</p>";
    h += "</div></article>";
    return h;
  }

  function compactCard(cape, st, now, edition) {
    var name = cape.names[edition] || cape.names.java;
    var h = '<article class="card compact" id="' + esc(edition + "-" + cape.id) + '">';
    h += '<div class="art" role="img" aria-label="' + esc(name) + ' texture" style="' + artStyle(cape.texture) + '"></div><div>';
    h += '<div class="title-row"><h3>' + esc(name) + "</h3>" + badge(st, now) + "</div>";
    h += '<p class="cat">' + esc(cape.category) + "</p>";
    h += statusLine(st, now, cape);
    h += '<p class="note">' + cape.steps.map(function (s) { return s; }).join(" ") + "</p>";
    (cape.alerts || []).forEach(function (a) { h += '<p class="alert-inline">⚠ ' + esc(a) + "</p>"; });
    h += windowsTable(cape);
    h += linksList(cape.links);
    h += '<p class="cite">Sources: ' + cites(cape.sources) + "</p>";
    h += "</div></article>";
    return h;
  }

  function group(title, desc, html) {
    if (!html) return "";
    return '<section class="group"><h2>' + esc(title) + "</h2>" + (desc ? '<p class="desc">' + esc(desc) + "</p>" : "") + html + "</section>";
  }

  // ---------- main render ----------

  function render() {
    var now = Date.now();
    var data = state.data;
    var edition = state.edition;
    state.sourceOrder = [];

    var live = [], upcoming = [], always = [], ended = [], hidden = 0;
    data.capes.forEach(function (cape) {
      if (cape.editions.indexOf(edition) === -1) return;
      var st = capeStatus(cape, now);
      if (isHidden(cape, st, now)) { hidden++; return; }
      var item = { cape: cape, st: st };
      if (st.code === "live") live.push(item);
      else if (st.code === "upcoming") upcoming.push(item);
      else if (st.code === "always") always.push(item);
      else ended.push(item);
    });

    live.sort(function (a, b) { return a.st.until - b.st.until; });
    upcoming.sort(function (a, b) { return (a.st.from || Infinity) - (b.st.from || Infinity); });
    ended.sort(function (a, b) { return b.st.closedAt - a.st.closedAt; });

    function cards(list, compact) {
      return list.map(function (x) { return (compact ? compactCard : fullCard)(x.cape, x.st, now, edition); }).join("");
    }

    var html = "";
    html += group("Limited-time capes: available now", "Sorted by which ends first.", cards(live)) ||
      group("Limited-time capes: available now", "", '<p class="empty">No limited-time capes are running right now.</p>');
    html += group("Upcoming", "", cards(upcoming));
    html += group("Always available", "", cards(always));
    html += group("Recently closed", "You can no longer earn these capes. Each one disappears from this list 2 months after it closes.", cards(ended, true));

    document.getElementById("panel").innerHTML = html;
    document.getElementById("hidden-count").textContent =
      hidden ? hidden + " cape" + (hidden === 1 ? " is" : "s are") + " currently hidden for " + EDITIONS[edition] + "." : "";

    renderSources();
  }

  function renderSources() {
    var srcs = state.data.sources;
    document.getElementById("sources").innerHTML = state.sourceOrder.map(function (id, i) {
      var s = srcs[id];
      return '<li id="src-' + (i + 1) + '"><a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title) +
        "</a> — " + esc(s.publisher) + (s.date ? ", " + esc(s.date) : "") + "</li>";
    }).join("");
  }

  function renderStatic() {
    var watch = state.data.watchLinks || [];
    document.getElementById("watch-links").innerHTML = watch.map(function (l) {
      return '<li><a href="' + esc(l.url) + '" target="_blank" rel="noopener">' + esc(l.label) + "</a></li>";
    }).join("");

    var parts = fmtDateTime.formatToParts(new Date());
    var tz = parts.filter(function (p) { return p.type === "timeZoneName"; })[0];
    if (tz) document.getElementById("tz-abbr").textContent = tz.value;

    var status = state.status;
    var checked = status && status.lastRun ? fmtDateTime.format(new Date(status.lastRun)) : "curated " + state.data.curatedOn;
    document.getElementById("last-checked").textContent = checked;

    var alerts = "";
    if (status && status.untrackedCapes && status.untrackedCapes.length) {
      alerts += '<div class="banner"><strong>New cape spotted:</strong> ' + status.untrackedCapes.map(function (c) {
        return '<a href="' + esc(c.url) + '" target="_blank" rel="noopener">' + esc(c.name) + "</a>";
      }).join(", ") + ". The Minecraft Wiki lists it, but full details haven't been added here yet.</div>";
    }
    if (status && status.capeNews && status.capeNews.length) {
      alerts += '<div class="banner"><strong>Recent cape news on Minecraft.net:</strong> ' + status.capeNews.slice(0, 3).map(function (n) {
        return '<a href="' + esc(n.url) + '" target="_blank" rel="noopener">' + esc(n.title) + "</a>";
      }).join(" · ") + "</div>";
    }
    document.getElementById("alerts").innerHTML = alerts;

    if (status && status.news && status.news.length) {
      document.getElementById("news-block").hidden = false;
      document.getElementById("news").innerHTML = status.news.slice(0, 8).map(function (n) {
        return "<li><time>" + esc(fmtShortDate.format(new Date(n.date))) + '</time><a href="' + esc(n.url) +
          '" target="_blank" rel="noopener">' + esc(n.title) + "</a>" + (n.mentionsCape ? ' <span class="badge live">cape</span>' : "") + "</li>";
      }).join("");
    }
  }

  function setEdition(edition, push) {
    state.edition = edition;
    document.querySelectorAll(".tabs button").forEach(function (b) {
      b.setAttribute("aria-selected", b.dataset.edition === edition ? "true" : "false");
    });
    if (push) history.replaceState(null, "", "#" + edition);
    render();
  }

  function tick() {
    var now = Date.now();
    document.querySelectorAll(".countdown[data-until]").forEach(function (el) {
      var until = +el.dataset.until;
      if (until <= now) { render(); return; }
      el.textContent = "Ends in " + countdown(until - now);
    });
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(url + ": " + r.status);
      return r.json();
    });
  }

  document.querySelectorAll(".tabs button").forEach(function (b) {
    b.addEventListener("click", function () { setEdition(b.dataset.edition, true); });
  });

  Promise.all([getJSON("data/capes.json"), getJSON("data/status.json").catch(function () { return null; })])
    .then(function (res) {
      state.data = res[0];
      state.status = res[1];
      var hash = location.hash.replace("#", "");
      renderStatic();
      setEdition(EDITIONS[hash] ? hash : "java", false);
      setInterval(tick, 30000);
    })
    .catch(function (err) {
      document.getElementById("panel").innerHTML = '<p class="banner">Could not load cape data: ' + esc(err.message) + "</p>";
    });
})();
