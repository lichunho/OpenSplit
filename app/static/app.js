// Equal/exact toggle + a live "remaining" counter on the expense form.
// Convenience only: the server re-validates everything, so this form still
// works correctly with JS disabled.
(function () {
  var form = document.getElementById("expense-form");
  if (!form) return;

  var amountInput = form.querySelector("#amount");
  var splitInputs = form.querySelectorAll('input[name="split_type"]');
  var shareInputs = form.querySelectorAll(".share-input");
  var remaining = document.getElementById("remaining");

  function currentSplitType() {
    for (var i = 0; i < splitInputs.length; i++) {
      if (splitInputs[i].checked) return splitInputs[i].value;
    }
    return "equal";
  }

  function updateVisibility() {
    var exact = currentSplitType() === "exact";
    form.classList.toggle("split-exact", exact);
    form.classList.toggle("split-equal", !exact);
    if (!exact && remaining) remaining.textContent = "";
  }

  function updateRemaining() {
    if (currentSplitType() !== "exact" || !remaining) return;
    var total = parseFloat(amountInput.value) || 0;
    var used = 0;
    for (var i = 0; i < shareInputs.length; i++) {
      var row = shareInputs[i].closest(".participant-row");
      var checkbox = row && row.querySelector(".participant-check");
      if (checkbox && checkbox.checked) used += parseFloat(shareInputs[i].value) || 0;
    }
    var left = Math.round((total - used) * 100) / 100;
    remaining.textContent = "Remaining: " + left.toFixed(2);
  }

  for (var i = 0; i < splitInputs.length; i++) {
    splitInputs[i].addEventListener("change", function () {
      updateVisibility();
      updateRemaining();
    });
  }
  amountInput.addEventListener("input", updateRemaining);
  for (var j = 0; j < shareInputs.length; j++) {
    shareInputs[j].addEventListener("input", updateRemaining);
  }
  form.querySelectorAll(".participant-check").forEach(function (cb) {
    cb.addEventListener("change", updateRemaining);
  });

  updateVisibility();
  updateRemaining();
})();

// Settle-up step 2: live "Settles you up" / "You'll still owe" hint as the
// amount is edited. Convenience only — the server doesn't depend on this;
// with JS off, the hint stays at whatever the server rendered for the
// pre-filled amount, and the form still submits and validates correctly.
(function () {
  var form = document.getElementById("settle-form");
  if (!form) return;

  var amountInput = form.querySelector("#amount");
  var hint = document.getElementById("settle-hint");
  var suggestedCents = parseInt(form.dataset.suggestedCents, 10) || 0;
  var payeeName = form.dataset.payeeName || "";
  if (!hint || suggestedCents <= 0) return;

  function updateHint() {
    var entered = Math.round((parseFloat(amountInput.value) || 0) * 100);
    if (entered <= 0) {
      hint.textContent = "";
    } else if (entered === suggestedCents) {
      hint.textContent = "Settles you up with " + payeeName + ".";
    } else if (entered < suggestedCents) {
      hint.textContent = "You'll still owe " + payeeName + " $" +
        ((suggestedCents - entered) / 100).toFixed(2) + ".";
    } else {
      hint.textContent = payeeName + " will owe you $" +
        ((entered - suggestedCents) / 100).toFixed(2) + ".";
    }
  }

  amountInput.addEventListener("input", updateHint);
  updateHint();
})();

// Cold-start affordance: Render's free tier spins down after 15 min idle and
// Neon after 5, so the first request to a sleeping app can take 60-90s. If a
// form submit or an internal link click hasn't navigated away within ~2s,
// say so instead of leaving what looks like a dead page. A warm response
// replaces the document before the timer ever fires, so this never shows up
// on a fast server, and nothing here delays or blocks the navigation itself
// (no preventDefault) — it's purely an indicator layered on top.
(function () {
  var banner = document.getElementById("loading-indicator");
  if (!banner) return;

  function armTimer() {
    // If navigation completes first, the document (and this timer) is torn
    // down with it, so the callback below simply never runs.
    setTimeout(function () {
      banner.hidden = false;
    }, 2000);
  }

  document.addEventListener("submit", function (event) {
    if (event.target && event.target.tagName === "FORM") armTimer();
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href]") : null;
    if (!link) return;
    var href = link.getAttribute("href");
    // Skip anything that won't actually replace this document: same-page
    // anchors, new-tab links, and downloads (export.csv) — a download never
    // navigates away, so the banner would show and then never go away.
    if (!href || href.charAt(0) === "#") return;
    if (link.target === "_blank" || link.hasAttribute("download")) return;
    if (/^https?:\/\//i.test(href)) return; // external link, different origin
    armTimer();
  });
})();

// Copy-link: only revealed once the Clipboard API is confirmed usable (it
// requires a secure context, so it stays hidden on plain http:// LAN dev).
// The share-link <input> is always there and always selectable via its own
// onclick="this.select()", so there's never a moment with a button that
// looks like it should work but silently does nothing.
(function () {
  var button = document.getElementById("copy-link-btn");
  var input = document.getElementById("link");
  var status = document.getElementById("copy-link-status");
  if (!button || !input) return;
  if (!window.isSecureContext || !navigator.clipboard) return;

  button.hidden = false;
  button.addEventListener("click", function () {
    navigator.clipboard.writeText(input.value).then(
      function () {
        if (status) status.textContent = "Copied!";
      },
      function () {
        // Clipboard write can still fail (permissions, etc) even when the
        // API exists — fall back to the same manual-select the input always
        // supports rather than leaving the click looking like a no-op.
        input.select();
        if (status) status.textContent = "";
      }
    );
  });
})();
