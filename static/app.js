// 封口线候选决策树接管试算平台 —— 前端交互
const $ = (id) => document.getElementById(id);

let pollTimers = new Map();
let prunePollTimers = new Map();

async function api(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({ ok: false, error: "响应不是合法 JSON" }));
  if (!resp.ok || !body.ok) {
    throw new Error(body.error || `请求失败（HTTP ${resp.status}）`);
  }
  return body;
}

function showMsg(el, text, ok = true) {
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
}

function parseJsonArea(el, label) {
  try {
    return JSON.parse(el.value);
  } catch (err) {
    throw new Error(`${label}不是合法 JSON：${err.message}`);
  }
}

function parseInterval(text, label) {
  let value;
  try {
    value = JSON.parse(text);
  } catch (err) {
    throw new Error(`${label}不是合法 JSON 数组`);
  }
  if (!Array.isArray(value) || value.length !== 2) {
    throw new Error(`${label}必须是 [下限, 上限] 形式`);
  }
  return value;
}

// ---------- 示例 ----------
async function loadExamples() {
  const data = await api("/api/examples");
  const box = $("examples");
  box.innerHTML = "";
  data.examples.forEach((ex) => {
    const btn = document.createElement("button");
    btn.textContent = ex.title;
    btn.onclick = () => {
      $("original-tree").value = JSON.stringify(ex.original_tree, null, 2);
      $("candidate-tree").value = JSON.stringify(ex.candidate_tree, null, 2);
      $("d-temp").value = JSON.stringify(ex.draft.temperature);
      $("d-front").value = JSON.stringify(ex.draft.front);
      $("d-back").value = JSON.stringify(ex.draft.back);
      $("d-diff").value = JSON.stringify(ex.draft.diff);
      showMsg($("draft-msg"), `已载入「${ex.title}」，请依次保存原树、保存草稿后发起试算`, true);
    };
    box.appendChild(btn);
  });

  const pdata = await api("/api/pruning-examples");
  const pbox = $("pruning-examples");
  pbox.innerHTML = "";
  pdata.examples.forEach((ex) => {
    const btn = document.createElement("button");
    btn.textContent = ex.title;
    btn.onclick = () => {
      $("candidate-tree").value = JSON.stringify(ex.candidate_tree, null, 2);
      $("prune-max-leaves").value = ex.max_leaves;
      $("prune-samples").value = JSON.stringify(ex.samples, null, 2);
      showMsg($("prune-msg"),
        `已载入「${ex.title}」，请先在第 ③ 区保存候选树，再到本区发起剪枝`, true);
    };
    pbox.appendChild(btn);
  });
}

// ---------- 保存原树 ----------
$("btn-save-original").onclick = async () => {
  try {
    const tree = parseJsonArea($("original-tree"), "原树");
    const data = await api("/api/original-tree", {
      method: "POST",
      body: JSON.stringify({ tree }),
    });
    showMsg($("original-msg"), `原树已保存（revision=${data.revision}），旧试算均已过期`, true);
    await refreshState();
  } catch (err) {
    showMsg($("original-msg"), err.message, false);
  }
};

// ---------- 保存候选树 + 草稿 ----------
$("btn-save-draft").onclick = async () => {
  try {
    const candidate_tree = parseJsonArea($("candidate-tree"), "候选树");
    const draft = {
      temperature: parseInterval($("d-temp").value, "温度区间"),
      front: parseInterval($("d-front").value, "前压区间"),
      back: parseInterval($("d-back").value, "后压区间"),
      diff: parseInterval($("d-diff").value, "压差区间"),
    };
    const data = await api("/api/draft", {
      method: "POST",
      body: JSON.stringify({ candidate_tree, draft }),
    });
    showMsg($("draft-msg"), `候选树与工况草稿已保存（revision=${data.revision}），试算期间也可继续保存`, true);
    await refreshState();
  } catch (err) {
    showMsg($("draft-msg"), err.message, false);
  }
};

// ---------- 发起试算 ----------
$("btn-trial").onclick = async () => {
  try {
    const data = await api("/api/trials", { method: "POST" });
    showMsg($("draft-msg"), `试算已发起（任务 ${data.job_id.slice(0, 8)}…，基于 revision=${data.revision}）`, true);
    pollJob(data.job_id);
    setTimeout(refreshState, 50);
  } catch (err) {
    showMsg($("draft-msg"), err.message, false);
  }
};

function pollJob(jobId) {
  if (pollTimers.has(jobId)) return;
  const timer = setInterval(async () => {
    try {
      const data = await api(`/api/trials/${jobId}`);
      await renderJobs();
      if (!["pending", "running"].includes(data.job.status)) {
        clearInterval(timer);
        pollTimers.delete(jobId);
        await refreshState();
      }
    } catch (err) {
      clearInterval(timer);
      pollTimers.delete(jobId);
    }
  }, 400);
  pollTimers.set(jobId, timer);
}

// ---------- 渲染任务列表 ----------
async function renderJobs() {
  const state = await api("/api/state");
  const box = $("jobs");
  if (!state.jobs.length) {
    box.innerHTML = '<p class="hint">暂无试算任务。</p>';
    return;
  }
  box.innerHTML = '<div class="jobs"></div>';
  const list = box.querySelector(".jobs");
  // 新任务在前
  state.jobs.slice().reverse().forEach((job) => {
    const div = document.createElement("div");
    div.className = "job";
    const statusText = {
      pending: "排队中", running: "试算中", solved: "成功",
      no_solution: "无解", error: "失败",
    }[job.status] || job.status;
    let detail = "";
    if (job.status === "solved" && job.result) {
      detail = `<div class="zone">最长安全温区：[${job.result.zone[0]}, ${job.result.zone[1]}]，长度 ${job.result.length}</div>
                <div class="reason">${job.result.reason}</div>`;
    } else if (job.status === "no_solution" && job.result) {
      detail = `<div class="reason">${job.result.reason}</div>`;
    } else if (job.status === "error") {
      detail = `<div class="err-text">试算失败：${job.error ?? "未知错误"}；当前分流保持不变（沿用原树）。</div>`;
    } else {
      detail = `<div class="reason">任务进行中，请稍候…</div>`;
    }
    const staleTag = job.stale
      ? '<span class="stale">已过期（草稿已修改，确认将被拒绝）</span>'
      : '<span class="pill">当前版本</span>';
    const confirmBtn = (job.status === "solved" && !job.stale)
      ? `<button data-confirm="${job.id}">确认按此试算接管</button>` : "";
    div.innerHTML = `
      <div class="job-head">
        <span class="pill ${job.status}">${statusText}</span>
        <span>任务 ${job.id.slice(0, 8)}…</span>
        <span>基于 revision=${job.revision}</span>
        ${staleTag}
        ${confirmBtn}
      </div>${detail}`;
    list.appendChild(div);
  });
  list.querySelectorAll("button[data-confirm]").forEach((btn) => {
    btn.onclick = () => confirmTrial(btn.dataset.confirm);
  });
}

async function confirmTrial(jobId) {
  try {
    const data = await api("/api/confirmations", {
      method: "POST",
      body: JSON.stringify({ trial_id: jobId }),
    });
    const c = data.confirmation;
    showMsg($("draft-msg"),
      `已确认：温区 [${c.zone[0]},${c.zone[1]}] 内且符合获准工况的读数改用候选树（revision=${data.revision}）`, true);
    await refreshState();
  } catch (err) {
    showMsg($("draft-msg"), "确认被拒：" + err.message, false);
    await refreshState();
  }
}

// ---------- 确认信息 ----------
function renderConfirmation(state) {
  const el = $("confirmation");
  const badge = $("confirm-badge");
  const c = state.confirmation;
  if (!c) {
    el.className = "confirmation empty";
    el.textContent = "尚无确认方案，当前所有读数均由原树分流。";
    badge.classList.add("hidden");
    return;
  }
  badge.classList.remove("hidden");
  el.className = "confirmation";
  const d = c.draft;
  el.textContent =
    `确认时间：${c.confirmed_at}\n` +
    `接管温区（整数闭区间）：[${c.zone[0]}, ${c.zone[1]}]\n` +
    `获准工况 —— 前压 [${d.front[0]}, ${d.front[1]}]，` +
    `后压 [${d.back[0]}, ${d.back[1]}]，` +
    `后压−前压 [${d.diff[0]}, ${d.diff[1]}]\n` +
    `来源试算：${c.trial_id.slice(0, 8)}…（确认时 revision=${c.revision}）\n` +
    `规则：只有温区内且同时满足上述获准工况的读数使用候选树，其余读数一律使用原树。`;
}

// ---------- 候选树剪枝 ----------
$("btn-prune").onclick = async () => {
  try {
    const maxLeaves = Number($("prune-max-leaves").value);
    if (!Number.isInteger(maxLeaves)) {
      throw new Error("叶数上限必须是整数");
    }
    let samples;
    try {
      samples = JSON.parse($("prune-samples").value);
    } catch (err) {
      throw new Error(`样品不是合法 JSON：${err.message}`);
    }
    const data = await api("/api/pruning", {
      method: "POST",
      body: JSON.stringify({ max_leaves: maxLeaves, samples }),
    });
    showMsg($("prune-msg"),
      `剪枝已发起（任务 ${data.job_id.slice(0, 8)}…，基于 revision=${data.revision}，样品 ${data.sample_count} 条）`, true);
    pollPruningJob(data.job_id);
    setTimeout(renderPruningJobs, 50);
  } catch (err) {
    showMsg($("prune-msg"), err.message, false);
  }
};

function pollPruningJob(jobId) {
  if (prunePollTimers.has(jobId)) return;
  const timer = setInterval(async () => {
    try {
      const data = await api(`/api/pruning/${jobId}`);
      await renderPruningJobs();
      if (!["pending", "running"].includes(data.job.status)) {
        clearInterval(timer);
        prunePollTimers.delete(jobId);
        await refreshState();
      }
    } catch (err) {
      clearInterval(timer);
      prunePollTimers.delete(jobId);
    }
  }, 400);
  prunePollTimers.set(jobId, timer);
}

async function renderPruningJobs() {
  const state = await api("/api/state");
  const box = $("pruning-jobs");
  const jobs = state.pruning_jobs || [];
  if (!jobs.length) {
    box.innerHTML = '<p class="hint">暂无剪枝任务。</p>';
    return;
  }
  box.innerHTML = '<div class="jobs"></div>';
  const list = box.querySelector(".jobs");
  jobs.slice().reverse().forEach((job) => {
    const div = document.createElement("div");
    div.className = "job";
    const statusText = {
      pending: "排队中", running: "剪枝中", solved: "成功",
      no_solution: "无解", error: "失败",
    }[job.status] || job.status;
    let detail = "";
    if (job.status === "solved" && job.result) {
      const r = job.result;
      detail =
        `<div class="zone">叶数：${r.leaves_before} → ${r.leaves_after}` +
        `（上限 ${r.max_leaves}）　误分样品：${r.misclass_before} → ${r.misclass_after}` +
        `（共 ${r.sample_count} 条，锁定 ${r.locked_total} 条，违约 ${r.locked_violations}）</div>` +
        `<div class="reason">${r.reason}</div>` +
        `<details class="tree-details"><summary>查看剪枝后候选树 JSON</summary>` +
        `<pre class="result">${JSON.stringify(r.pruned_tree, null, 2)}</pre></details>`;
      if (job.applied) {
        detail += `<div class="msg ok">已用该结果替换草稿候选树（新 revision=${job.apply_revision}），旧温区试算已过期。</div>`;
      } else if (job.superseded) {
        detail += `<div class="err-text">${job.apply_reason ?? "版号已变化，未替换草稿，最新草稿已保留"}</div>`;
      }
    } else if (job.status === "no_solution" && job.result) {
      detail = `<div class="err-text">${job.result.reason}</div>
                <div class="reason">草稿保持不变。</div>`;
    } else if (job.status === "error") {
      detail = `<div class="err-text">剪枝失败：${job.error ?? "未知错误"}；草稿保持不变。</div>`;
    } else {
      detail = `<div class="reason">任务进行中，请稍候…（期间仍可保存草稿）</div>`;
    }
    const staleTag = job.stale
      ? '<span class="stale">版号已变化（草稿被改过）</span>'
      : '<span class="pill">当前版本</span>';
    div.innerHTML = `
      <div class="job-head">
        <span class="pill ${job.status}">${statusText}</span>
        <span>任务 ${job.id.slice(0, 8)}…</span>
        <span>基于 revision=${job.revision}</span>
        <span>样品 ${job.sample_count} 条 · 上限 ${job.max_leaves} 叶</span>
        ${staleTag}
      </div>${detail}`;
    list.appendChild(div);
  });
}

// ---------- 读数查询 ----------
$("btn-classify").onclick = async () => {
  try {
    const body = {
      temperature: Number($("r-temp").value),
      front: Number($("r-front").value),
      back: Number($("r-back").value),
    };
    const data = await api("/api/classify", { method: "POST", body: JSON.stringify(body) });
    $("classify-result").textContent = JSON.stringify({
      读数: data.readings,
      使用的树: data.tree_used === "candidate" ? "候选树（已确认接管）" : "原树",
      分拣类别: data.category,
      判定依据: data.reason,
    }, null, 2);
  } catch (err) {
    $("classify-result").textContent = "查询失败：" + err.message;
  }
};

// ---------- 总刷新 ----------
async function refreshState() {
  const state = await api("/api/state");
  $("revision").textContent = state.revision;
  renderConfirmation(state);
  await renderJobs();
  await renderPruningJobs();
}

loadExamples().catch((err) => showMsg($("draft-msg"), err.message, false));
refreshState().catch((err) => showMsg($("draft-msg"), err.message, false));
setInterval(renderJobs, 2000); // 轮询刷新任务状态与过期标记
setInterval(renderPruningJobs, 2000);
