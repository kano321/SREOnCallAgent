/* ============================================================
   SRE On-Call Agent · 登录页逻辑
   登录 / 注册 / 安全问题找回密码
   ============================================================ */

const $ = (sel) => document.querySelector(sel);

// 已登录则直接进监控台
if (localStorage.getItem("sre_token")) {
  location.href = "/";
}

function showMsg(elId, text, ok) {
  const el = document.getElementById(elId);
  el.textContent = text || "";
  el.className = "login-msg " + (ok ? "ok" : "err");
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
  return data;
}

/* ---------------- 标签切换 ---------------- */

document.querySelectorAll(".login-tabs button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".login-tabs button").forEach((b) =>
      b.classList.toggle("active", b === btn));
    document.querySelectorAll(".login-view").forEach((v) =>
      v.classList.toggle("active", v.id === "view-" + btn.dataset.view));
  };
});

/* ---------------- 登录 ---------------- */

$("#view-login").onsubmit = async (e) => {
  e.preventDefault();
  showMsg("loginMsg", "");
  try {
    const data = await api("/auth/login", {
      username: $("#loginUsername").value.trim(),
      password: $("#loginPassword").value,
    });
    localStorage.setItem("sre_token", data.token);
    localStorage.setItem("sre_username", data.username);
    location.href = "/";
  } catch (err) {
    showMsg("loginMsg", err.message);
  }
};

/* ---------------- 注册 ---------------- */

$("#view-register").onsubmit = async (e) => {
  e.preventDefault();
  showMsg("regMsg", "");
  try {
    await api("/auth/register", {
      username: $("#regUsername").value.trim(),
      password: $("#regPassword").value,
      question: $("#regQuestion").value.trim(),
      answer: $("#regAnswer").value.trim(),
    });
    showMsg("regMsg", "注册成功，请前往「登录」", true);
    $("#view-register").reset();
  } catch (err) {
    showMsg("regMsg", err.message);
  }
};

/* ---------------- 找回密码 ---------------- */

$("#forgotNext").onclick = async () => {
  const username = $("#forgotUsername").value.trim();
  if (!username) { showMsg("forgotMsg1", "请输入用户名"); return; }
  showMsg("forgotMsg1", "");
  try {
    const data = await api("/auth/forgot/question", { username });
    $("#forgotQuestion").textContent = data.question;
    $("#forgotStep1").style.display = "none";
    $("#forgotStep2").style.display = "";
  } catch (err) {
    showMsg("forgotMsg1", err.message);
  }
};

$("#forgotBack").onclick = () => {
  $("#forgotStep2").style.display = "none";
  $("#forgotStep1").style.display = "";
  showMsg("forgotMsg2", "");
};

$("#view-forgot").onsubmit = async (e) => {
  e.preventDefault();
  showMsg("forgotMsg2", "");
  try {
    await api("/auth/forgot/reset", {
      username: $("#forgotUsername").value.trim(),
      answer: $("#forgotAnswer").value.trim(),
      new_password: $("#forgotNewPassword").value,
    });
    showMsg("forgotMsg2", "密码已重置，请用新密码登录", true);
    setTimeout(() => {
      document.querySelectorAll(".login-tabs button")[0].click();
      $("#view-forgot").reset();
      $("#forgotStep2").style.display = "none";
      $("#forgotStep1").style.display = "";
    }, 1200);
  } catch (err) {
    showMsg("forgotMsg2", err.message);
  }
};
