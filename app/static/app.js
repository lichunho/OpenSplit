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
