(function () {
  var pw = document.getElementById("pw"), go = document.getElementById("go"), msg = document.getElementById("msg");
  function submit() {
    msg.textContent = ""; go.disabled = true;
    fetch("/__share_login", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "dashboard" }, body: JSON.stringify({ password: pw.value }) })
      .then(function (r) { return r.json().then(function (j) { if (r.ok && j.ok) location.reload(); else { msg.textContent = j.error || "密码不对"; go.disabled = false; } }); })
      .catch(function () { msg.textContent = "网络出错，请重试"; go.disabled = false; });
  }
  go.addEventListener("click", submit);
  pw.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
})();
