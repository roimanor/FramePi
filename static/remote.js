(function () {
  var statusEl = document.getElementById("remote-status");
  var nowModeEl = document.getElementById("remote-now-mode");
  var nowMetaEl = document.getElementById("remote-now-meta");
  var slideTitleEl = document.getElementById("remote-slide-title");
  var slideLocationEl = document.getElementById("remote-slide-location");
  var previewSlot = document.getElementById("remote-preview-slot");
  var previewPlaceholder = document.getElementById("remote-preview-placeholder");
  var previewImg = document.getElementById("remote-preview-img");
  var previewVideo = document.getElementById("remote-preview-video");
  var editForm = document.getElementById("remote-edit");
  var editTab = document.getElementById("remote-edit-tab");
  var editPanel = document.getElementById("remote-edit-panel");
  var editFilename = document.getElementById("remote-edit-filename");
  var editName = document.getElementById("remote-edit-name");
  var editDate = document.getElementById("remote-edit-date");
  var editLocation = document.getElementById("remote-edit-location");
  var deleteBtn = document.getElementById("remote-edit-delete");
  var slideSecondsInput = document.getElementById("remote-slide-seconds");
  var slideApplyBtn = document.getElementById("remote-slide-apply");
  var pauseBtn = document.getElementById("remote-pause-btn");
  var modeBadge = document.getElementById("remote-mode-badge");
  var headerHint = document.getElementById("remote-header-hint");
  var panelGallery = document.getElementById("remote-controls-gallery");
  var panelMap = document.getElementById("remote-controls-map");

  if (panelMap) {
    panelMap.querySelectorAll('[data-remote-key="Enter"]').forEach(function (btn) {
      btn.remove();
    });
  }

  var currentItem = null;
  var currentPreview = null;
  var slideshowPaused = false;
  var lastNowSeq = 0;

  function shouldApplyNow(payload) {
    if (!payload || payload.now_seq == null) return true;
    var seq = Number(payload.now_seq);
    if (!isFinite(seq)) return true;
    if (seq < lastNowSeq) return false;
    lastNowSeq = seq;
    return true;
  }

  function setRemoteMode(mode) {
    var body = document.body;
    if (!body) return;
    body.classList.remove(
      "remote-mode-waiting",
      "remote-mode-gallery",
      "remote-mode-map",
      "remote-mode-empty"
    );
    body.classList.add("remote-mode-" + (mode || "waiting"));

    if (panelGallery) panelGallery.hidden = mode !== "gallery";
    if (panelMap) panelMap.hidden = mode !== "map";

    if (modeBadge) {
      if (mode === "gallery") modeBadge.textContent = "Slideshow";
      else if (mode === "map") modeBadge.textContent = "Map";
      else if (mode === "empty") modeBadge.textContent = "Empty";
      else modeBadge.textContent = "Connecting";
    }
    if (headerHint) {
      if (mode === "gallery") headerHint.textContent = "Warm gallery mode — browse and edit photos.";
      else if (mode === "map") headerHint.textContent = "Cool map mode — pick a place, then return to photos.";
      else headerHint.textContent = "Control your frame on the same Wi‑Fi.";
    }
  }

  function itemDisplayTitle(item) {
    if (!item || item.name == null) return "";
    return String(item.name).trim();
  }

  function setSlideTitle(text) {
    if (!slideTitleEl) return;
    var t = text != null ? String(text).trim() : "";
    slideTitleEl.textContent = t || "—";
    slideTitleEl.classList.toggle("remote-slide-title-text--empty", !t);
  }

  function formatLocationLine(item) {
    if (!item) return "";
    var parts = [];
    var loc = String(item.location || "").trim();
    if (loc) parts.push(loc);
    var place = [item.city, item.country].filter(Boolean).join(", ");
    if (place && parts.indexOf(place) < 0) parts.push(place);
    return parts.join(" · ");
  }

  function setSlideLocation(text) {
    if (!slideLocationEl) return;
    var t = text != null ? String(text).trim() : "";
    slideLocationEl.textContent = t || "—";
    slideLocationEl.classList.toggle("remote-slide-meta-text--empty", !t);
  }

  function fillMetaFromItem(item, lines) {
    clearMeta();
    setSlideTitle(item ? itemDisplayTitle(item) : "");
    setSlideLocation(item ? formatLocationLine(item) : "");

    if (Array.isArray(lines) && lines.length) {
      lines.forEach(function (line, i) {
        if (i === 0) return;
        if (line.indexOf("lat ") === 0) {
          addMetaRow("GPS", line);
        } else if (line.match(/^\d{4}/)) {
          addMetaRow("Date", line);
        }
      });
      return;
    }
    if (!item) return;
    if (item.gps_latitude != null && item.gps_longitude != null) {
      addMetaRow(
        "GPS",
        Number(item.gps_latitude).toFixed(6) + ", " + Number(item.gps_longitude).toFixed(6)
      );
    }
    if (item.created_time) addMetaRow("Date", item.created_time);
    if (item.media_type) addMetaRow("Type", item.media_type);
  }

  function setStatus(msg, isErr) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.classList.toggle("remote-status--error", Boolean(isErr));
  }

  function clearMeta() {
    if (!nowMetaEl) return;
    while (nowMetaEl.firstChild) {
      nowMetaEl.removeChild(nowMetaEl.firstChild);
    }
  }

  function addMetaRow(label, value) {
    if (!nowMetaEl || value == null || value === "") return;
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = String(value);
    nowMetaEl.appendChild(dt);
    nowMetaEl.appendChild(dd);
  }

  function itemIsVideo(item) {
    if (!item) return false;
    if (item.media_type === "video") return true;
    var mime = String(item.mime_type || "").toLowerCase();
    if (mime.indexOf("video/") === 0) return true;
    var lp = String(item.local_path || "").toLowerCase();
    return /\.(mp4|webm|mov|m4v|mkv)$/.test(lp);
  }

  function clearPreviewMedia() {
    if (previewImg) previewImg.removeAttribute("src");
    if (previewVideo) {
      previewVideo.pause();
      previewVideo.removeAttribute("src");
      previewVideo.load();
    }
    currentPreview = null;
  }

  function setPreviewPlaceholder(text) {
    if (!previewSlot) return;
    previewSlot.classList.remove("remote-preview-slot--image", "remote-preview-slot--video");
    clearPreviewMedia();
    if (previewPlaceholder) previewPlaceholder.textContent = text;
  }

  function showPreview(item, preview) {
    if (!previewSlot || !item) {
      setPreviewPlaceholder("No preview");
      return;
    }
    var p = preview || {};
    var googleUrl = p.google || "";
    var localUrl = p.local || "";
    if (!googleUrl && !localUrl) {
      setPreviewPlaceholder("Preview unavailable");
      return;
    }

    currentPreview = { google: googleUrl, local: localUrl };
    var isVideo = itemIsVideo(item);

    previewSlot.classList.remove("remote-preview-slot--image", "remote-preview-slot--video");

    if (isVideo && previewVideo) {
      previewSlot.classList.add("remote-preview-slot--video");
      if (previewImg) previewImg.removeAttribute("src");
      previewVideo.onerror = function () {
        if (localUrl && previewVideo.getAttribute("src") !== localUrl) {
          previewVideo.src = localUrl;
        }
      };
      previewVideo.src = googleUrl || localUrl;
      previewVideo.load();
    } else if (previewImg) {
      previewSlot.classList.add("remote-preview-slot--image");
      if (previewVideo) {
        previewVideo.pause();
        previewVideo.removeAttribute("src");
        previewVideo.load();
      }
      previewImg.alt = itemDisplayTitle(item) || "Current slide";
      previewImg.onerror = function () {
        if (localUrl && previewImg.getAttribute("src") !== localUrl) {
          previewImg.src = localUrl;
        }
      };
      previewImg.src = googleUrl || localUrl;
    }
  }

  function updatePauseButton(paused, show) {
    if (!pauseBtn) return;
    pauseBtn.classList.toggle("remote-icon-btn--inactive", !show);
    pauseBtn.disabled = !show;
    pauseBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
    pauseBtn.setAttribute("aria-pressed", paused ? "true" : "false");
    pauseBtn.classList.toggle("remote-icon-btn--is-paused", Boolean(paused));
  }

  function setEditPanelOpen(open) {
    if (!editPanel || !editTab) return;
    var isOpen = Boolean(open);
    editPanel.hidden = !isOpen;
    editTab.setAttribute("aria-expanded", isOpen ? "true" : "false");
    var label = editTab.querySelector(".remote-edit-tab-label");
    if (label) label.textContent = isOpen ? "Hide details" : "Edit details";
  }

  function showEditForm(item, preview, keepPanelOpen) {
    if (!editForm || !item || !item.local_path) {
      if (editForm) editForm.hidden = true;
      setEditPanelOpen(false);
      currentItem = null;
      setPreviewPlaceholder("No slide");
      return;
    }
    currentItem = item;
    editForm.hidden = false;
    if (!keepPanelOpen) setEditPanelOpen(false);
    showPreview(item, preview);
    if (editFilename) {
      var fn = item.filename || item.local_path;
      editFilename.textContent = fn ? "File: " + fn : "";
    }
    if (editName) editName.value = item.name != null ? String(item.name) : "";
    if (editDate) editDate.value = item.created_time != null ? String(item.created_time) : "";
    if (editLocation) editLocation.value = item.location != null ? String(item.location) : "";
    var editCity = document.getElementById("remote-edit-city");
    var editCountry = document.getElementById("remote-edit-country");
    if (editCity) editCity.value = item.city != null ? String(item.city) : "";
    if (editCountry) editCountry.value = item.country != null ? String(item.country) : "";
  }

  function hideEditForm() {
    if (editForm) editForm.hidden = true;
    setEditPanelOpen(false);
    currentItem = null;
    updatePauseButton(false, false);
  }

  function renderNow(payload) {
    if (!payload || payload.type !== "now") return;
    if (!shouldApplyNow(payload)) return;
    clearMeta();
    var mode = payload.mode;
    setRemoteMode(mode === "gallery" || mode === "map" || mode === "empty" ? mode : "waiting");

    if (mode === "gallery") {
      var idx = Number(payload.index) || 0;
      var total = Number(payload.total) || 0;
      var pos = total > 0 ? idx + 1 + " / " + total : "";
      var filter = payload.on_this_day ? " · On this day" : "";
      slideshowPaused = Boolean(payload.paused);
      if (nowModeEl) {
        var modeText = "Slideshow" + (pos ? " · " + pos : "") + filter;
        if (slideshowPaused) modeText += " · Paused";
        nowModeEl.textContent = modeText;
      }
      var item = payload.item || {};
      showEditForm(item, payload.preview);
      updatePauseButton(slideshowPaused, true);
      fillMetaFromItem(item, payload.lines);
      return;
    }
    hideEditForm();
    if (mode === "map") {
      if (nowModeEl) nowModeEl.textContent = "Exploring the map";
      setPreviewPlaceholder("");
      var pin = payload.map_pin || {};
      setSlideTitle(pin.label || "");
      setSlideLocation(pin.summary || "");
      if (pin.total > 0) {
        addMetaRow("Pin", (Number(pin.index) || 0) + 1 + " / " + pin.total);
      }
      addMetaRow("Media", pin.summary);
      return;
    }
    if (mode === "empty") {
      if (nowModeEl) nowModeEl.textContent = "Nothing playing";
      setPreviewPlaceholder(payload.message || "No photos on the frame yet");
      setSlideTitle("");
      setSlideLocation("");
      addMetaRow("Status", payload.message || "Nothing to show");
    }
  }

  function connectNowStream() {
    if (typeof EventSource === "undefined") return;
    var es = new EventSource("/api/remote/stream");
    es.onmessage = function (ev) {
      try {
        var payload = JSON.parse(ev.data);
        if (payload.type === "settings") {
          if (slideSecondsInput && payload.slide_seconds != null) {
            slideSecondsInput.value = String(payload.slide_seconds);
          }
          return;
        }
        renderNow(payload);
      } catch (e) {
        /* ignore */
      }
    };
    es.onerror = function () {
      if (nowModeEl && !nowModeEl.textContent) {
        nowModeEl.textContent = "Reconnecting…";
      }
    };
  }

  fetch("/api/remote/now")
    .then(function (r) {
      return r.json();
    })
    .then(function (payload) {
      if (payload && payload.type === "now") {
        renderNow(payload);
      }
    })
    .catch(function () {
      /* ignore */
    });

  function loadSlideSettings() {
    return fetch("/api/remote/settings")
      .then(function (r) {
        return r.json();
      })
      .then(function (body) {
        if (slideSecondsInput && body.slide_seconds != null) {
          slideSecondsInput.value = String(body.slide_seconds);
        }
      })
      .catch(function () {
        /* ignore */
      });
  }

  function applySlideSettings() {
    if (!slideSecondsInput) return;
    var sec = parseFloat(slideSecondsInput.value);
    if (!isFinite(sec) || sec < 0.5) {
      setStatus("Enter at least 0.5 seconds.", true);
      return;
    }
    setStatus("Updating slide timing…", false);
    fetch("/api/remote/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide_seconds: sec }),
    })
      .then(function (r) {
        return r.json().then(function (body) {
          return { ok: r.ok, body: body };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          setStatus(res.body.error || "Could not update timing", true);
          return;
        }
        if (slideSecondsInput && res.body.slide_seconds != null) {
          slideSecondsInput.value = String(res.body.slide_seconds);
        }
        setStatus("Slide speed set to " + res.body.slide_seconds + " seconds.", false);
      })
      .catch(function (err) {
        setStatus(String(err), true);
      });
  }

  if (slideApplyBtn) {
    slideApplyBtn.addEventListener("click", applySlideSettings);
  }

  if (pauseBtn) {
    pauseBtn.addEventListener("click", function () {
      sendKey("TogglePause");
    });
  }

  loadSlideSettings();
  connectNowStream();

  if (editTab) {
    editTab.addEventListener("click", function () {
      if (!editPanel) return;
      setEditPanelOpen(editPanel.hidden);
    });
  }

  if (editForm) {
    editForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!currentItem || !currentItem.local_path) {
        setStatus("Nothing to save (not on a slide).", true);
        return;
      }
      setStatus("Saving…", false);
      fetch("/api/remote/item", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          local_path: currentItem.local_path,
          id: currentItem.id,
          name: editName ? editName.value : "",
          created_time: editDate ? editDate.value : "",
          location: editLocation ? editLocation.value : "",
          city: document.getElementById("remote-edit-city")
            ? document.getElementById("remote-edit-city").value
            : "",
          country: document.getElementById("remote-edit-country")
            ? document.getElementById("remote-edit-country").value
            : "",
        }),
      })
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            setStatus(res.body.error || "Save failed", true);
            return;
          }
          setStatus("Details saved.", false);
          if (res.body.item) {
            var panelWasOpen = editPanel && !editPanel.hidden;
            currentItem = res.body.item;
            showEditForm(res.body.item, currentPreview, panelWasOpen);
            fillMetaFromItem(res.body.item, null);
          }
        })
        .catch(function (err) {
          setStatus(String(err), true);
        });
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", function () {
      if (!currentItem || !currentItem.local_path) {
        setStatus("Nothing to remove (not on a slide).", true);
        return;
      }
      var label =
        (currentItem.name && String(currentItem.name).trim()) ||
        currentItem.filename ||
        currentItem.local_path;
      if (!window.confirm("Remove this photo from the frame?\n\n" + label)) {
        return;
      }
      setStatus("Removing…", false);
      fetch("/api/remote/item", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          local_path: currentItem.local_path,
          id: currentItem.id,
        }),
      })
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            setStatus(res.body.error || "Delete failed", true);
            return;
          }
          setStatus("Removed from frame.", false);
          hideEditForm();
        })
        .catch(function (err) {
          setStatus(String(err), true);
        });
    });
  }

  function sendKey(key) {
    setStatus("Sending…", false);
    fetch("/api/remote/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: key }),
    })
      .then(function (r) {
        return r.json().then(function (body) {
          return { ok: r.ok, body: body };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          setStatus(res.body.error || "Request failed", true);
          return;
        }
        var n = res.body.delivered_to_streams;
        if (n === 0) {
          setStatus("Sent — no viewer subscribed (is app.py running on the Pi?)", true);
        } else {
          setStatus("Sent to frame (" + n + " tab" + (n === 1 ? "" : "s") + ").", false);
        }
      })
      .catch(function (err) {
        setStatus(String(err), true);
      });
  }

  document.querySelectorAll("[data-remote-key]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      sendKey(btn.getAttribute("data-remote-key"));
    });
  });

  var syncBtn = document.getElementById("remote-sync-album");
  if (syncBtn) {
    syncBtn.addEventListener("click", function () {
      setStatus("Starting album sync…", false);
      fetch("/api/sync", { method: "POST" })
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            setStatus(res.body.error || "Sync request failed", true);
            return;
          }
          setStatus(res.body.message || "Sync started.", false);
        })
        .catch(function (err) {
          setStatus(String(err), true);
        });
    });
  }
})();
