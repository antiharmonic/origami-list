// Global notes panel: one markdown document, autosaved. `rev` guards against two windows overwriting each other.
(function () {
  var toggle = document.getElementById("notes-toggle");
  var panel = document.getElementById("notes-panel");
  var backdrop = document.getElementById("notes-backdrop");
  var text = document.getElementById("notes-text");
  var preview = document.getElementById("notes-preview");
  var status = document.getElementById("notes-status");
  var tabs = panel.querySelectorAll(".notes-tabs button");
  var rev = null, dirty = false, saving = false, again = false, conflict = false, timer = null;

  function setStatus(msg) { status.textContent = msg; }

  function load() {
    return fetch("/notes").then(function (r) { return r.json(); }).then(function (d) {
      text.value = d.body; rev = d.rev; dirty = false; conflict = false; setStatus("");
    });
  }

  function form(force) {
    var f = new FormData();
    f.append("body", text.value);
    if (rev !== null) f.append("rev", rev);
    if (force) f.append("force", "true");
    return f;
  }

  function save(force) {
    clearTimeout(timer);
    if (conflict && !force) return;
    if (saving) { again = true; return; }
    saving = true;
    var body = text.value;
    setStatus("Saving…");
    fetch("/notes", { method: "POST", body: form(force) }).then(function (r) {
      return r.json().then(function (d) {
        if (r.status === 409) { showConflict(); return; }
        rev = d.rev; conflict = false;
        if (text.value === body) dirty = false;
        setStatus("Saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      });
    }).catch(function () {
      setStatus("Save failed — will retry on next edit");
    }).finally(function () {
      saving = false;
      if (again) { again = false; if (dirty) save(); }
    });
  }

  function showConflict() {
    conflict = true;
    status.innerHTML = "Changed in another window. ";
    var theirs = document.createElement("button"), mine = document.createElement("button");
    theirs.type = mine.type = "button";
    theirs.textContent = "Load theirs";
    mine.textContent = "Keep mine";
    theirs.onclick = function () { load(); };
    mine.onclick = function () { save(true); };
    status.append(theirs, " ", mine);
  }

  function setMode(mode) {
    tabs.forEach(function (b) { b.classList.toggle("active", b.dataset.mode === mode); });
    text.hidden = mode !== "edit";
    preview.hidden = mode !== "preview";
    if (mode === "edit") { text.focus(); return; }
    var f = new FormData();
    f.append("body", text.value);
    fetch("/notes/preview", { method: "POST", body: f }).then(function (r) { return r.text(); }).then(function (html) {
      preview.innerHTML = html || '<p class="hint">Nothing yet.</p>';
      preview.querySelectorAll("a").forEach(function (a) { a.target = "_blank"; a.rel = "noopener"; });
    });
  }

  function open() {
    panel.hidden = backdrop.hidden = false;
    // Reload unless there are local edits still to send, so each open shows the latest from other windows.
    var ready = dirty || saving ? Promise.resolve() : load();
    ready.then(function () {
      setMode("edit");
      text.setSelectionRange(text.value.length, text.value.length);
      text.scrollTop = text.scrollHeight;
    });
  }

  function close() {
    if (dirty) save();
    panel.hidden = backdrop.hidden = true;
  }

  toggle.addEventListener("click", function () { panel.hidden ? open() : close(); });
  backdrop.addEventListener("click", close);
  panel.querySelector(".notes-close").addEventListener("click", close);
  tabs.forEach(function (b) { b.addEventListener("click", function () { setMode(b.dataset.mode); }); });

  document.addEventListener("keydown", function (e) {
    if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === "n") {
      e.preventDefault();
      panel.hidden ? open() : close();
    } else if (e.key === "Escape" && !panel.hidden) {
      close();
    }
  });

  // Flush unsaved edits when leaving the page (e.g. clicking a link with the panel open).
  window.addEventListener("pagehide", function () {
    if (dirty && !conflict) navigator.sendBeacon("/notes", form(false));
  });

  text.addEventListener("input", function () {
    dirty = true;
    if (!conflict) setStatus("Edited");
    clearTimeout(timer);
    timer = setTimeout(save, 800);
  });

  // --- editing helpers ---

  function insert(str) {
    // execCommand keeps the edit in the textarea's undo history.
    if (document.execCommand("insertText", false, str)) return;
    text.setRangeText(str, text.selectionStart, text.selectionEnd, "end");
    text.dispatchEvent(new Event("input"));
  }

  function lineStart(pos) { return text.value.lastIndexOf("\n", pos - 1) + 1; }

  function wrap(marker) {
    var s = text.selectionStart, e = text.selectionEnd;
    insert(marker + text.value.slice(s, e) + marker);
    if (s === e) text.setSelectionRange(s + marker.length, s + marker.length);
  }

  function indent(out) {
    var s = text.selectionStart, e = text.selectionEnd, start = lineStart(s);
    var end = text.value.indexOf("\n", e);
    if (end === -1) end = text.value.length;
    var lines = text.value.slice(start, end).split("\n");
    var changed = lines.map(function (l) { return out ? l.replace(/^ {1,2}/, "") : "  " + l; }).join("\n");
    text.setSelectionRange(start, end);
    insert(changed);
    text.setSelectionRange(start, start + changed.length);
  }

  text.addEventListener("keydown", function (e) {
    var mod = e.ctrlKey || e.metaKey;
    if (mod && !e.shiftKey && e.key === "b") { e.preventDefault(); wrap("**"); }
    else if (mod && !e.shiftKey && e.key === "i") { e.preventDefault(); wrap("*"); }
    else if (e.key === "Tab" && !mod && !e.altKey) { e.preventDefault(); indent(e.shiftKey); }
    else if (e.key === "Enter" && !mod && !e.shiftKey && !e.altKey && text.selectionStart === text.selectionEnd) {
      var pos = text.selectionStart, start = lineStart(pos);
      var m = text.value.slice(start, pos).match(/^(\s*)([-*+]|(\d+)\.)\s+(\[[ xX]\]\s+)?(.*)$/);
      if (!m) return;
      e.preventDefault();
      if (!m[5].trim()) {
        // Enter on an empty bullet ends the list.
        text.setSelectionRange(start, pos);
        insert("");
        return;
      }
      var marker = m[3] ? (Number(m[3]) + 1) + "." : m[2];
      insert("\n" + m[1] + marker + " " + (m[4] ? "[ ] " : ""));
    }
  });
})();
