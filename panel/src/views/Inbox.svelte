<script lang="ts">
  import { onMount, tick } from "svelte";
  import { Panel, events, type Budget, type Filter, type Job } from "../mygo";
  import { toast } from "../lib/toast.svelte";
  import {
    ago, blocks, company, description, errText, region, scoreColor, shortAge, statusMeta, title,
  } from "../lib/util";

  let { onchange }: { onchange: () => void } = $props();

  const ROW = 46;
  const PAGE = 300;

  let filter = $state<Filter>({
    status: "", source: "", market: "", remote: false, lang: "", minScore: 0, query: "", sort: "score", offset: 0, limit: PAGE,
  });
  let jobs = $state<Job[]>([]);
  let total = $state(0);
  let loading = $state(true);
  let cur = $state(0);
  let checked = $state(new Set<number>());
  let sources = $state<string[]>([]);
  let budget = $state<Budget | null>(null);
  let busy = $state(false);

  let editing = $state(false);
  let draft = $state({ title: "", company: "", description: "", notes: "" });
  let confirmSend = $state<{ ids: number[]; resend: boolean } | null>(null);

  let listEl = $state<HTMLDivElement>();
  let searchEl = $state<HTMLInputElement>();
  let scrollTop = $state(0);
  let viewH = $state(600);

  const job = $derived(jobs[cur]);
  const start = $derived(Math.max(0, Math.floor(scrollTop / ROW) - 6));
  const end = $derived(Math.min(jobs.length, start + Math.ceil(viewH / ROW) + 12));

  async function load(reset = true) {
    try {
      if (reset) loading = true;
      const page = await Panel.list({ ...filter, offset: reset ? 0 : jobs.length, limit: PAGE });
      total = page.total;
      jobs = reset ? page.jobs : [...jobs, ...page.jobs];
      if (cur >= jobs.length) cur = Math.max(0, jobs.length - 1);
      if (reset) {
        const ids = new Set(jobs.map((j) => j.id));
        checked = new Set([...checked].filter((id) => ids.has(id)));
      }
    } catch (e) {
      toast(errText(e), { kind: "error" });
    } finally {
      loading = false;
    }
  }

  async function loadMeta() {
    try {
      sources = await Panel.sources();
      budget = await Panel.budget();
    } catch {}
  }

  $effect(() => {
    // more rows as the list nears the end of what is loaded
    if (!loading && end > jobs.length - 40 && jobs.length < total) load(false);
  });

  let qTimer: ReturnType<typeof setTimeout>;
  function setFilter(patch: Partial<Filter>) {
    filter = { ...filter, ...patch };
    cur = 0;
    if (listEl) listEl.scrollTop = 0;
    clearTimeout(qTimer);
    qTimer = setTimeout(() => load(), patch.query !== undefined ? 180 : 0);
  }

  function select(i: number) {
    if (i < 0 || i >= jobs.length) return;
    if (editing) saveEdit();
    cur = i;
    const el = listEl;
    if (!el) return;
    const top = i * ROW;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (top + ROW > el.scrollTop + el.clientHeight) el.scrollTop = top + ROW - el.clientHeight;
  }

  function targets(): number[] {
    if (checked.size) return [...checked];
    return job ? [job.id] : [];
  }

  async function act(status: "shortlisted" | "dismissed" | "new") {
    const ids = targets();
    if (!ids.length || busy) return;
    busy = true;
    try {
      const n = await Panel.act(ids, status);
      const verb = { shortlisted: "Оставлено", dismissed: "Удалено в корзину", new: "Возвращено" }[status];
      toast(`${verb}: ${n}`, { action: { label: "Отменить", run: undo } });
      checked = new Set();
      await load();
      onchange();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    } finally {
      busy = false;
    }
  }

  async function undo() {
    try {
      const n = await Panel.undo();
      toast(`Отменено: ${n}`);
      await load();
      onchange();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }

  function askSend(resend = false) {
    const ids = targets();
    if (!ids.length) return;
    const list = ids.map((id) => jobs.find((j) => j.id === id)).filter(Boolean) as Job[];
    if (!resend && list.length && list.every((j) => j.status === "sent")) resend = true;
    confirmSend = { ids, resend };
  }

  async function doSend() {
    if (!confirmSend) return;
    const { ids, resend } = confirmSend;
    confirmSend = null;
    busy = true;
    try {
      const r = await Panel.send(ids, resend);
      budget = r.budget;
      const failed = r.failed.length > 0;
      toast((resend ? "Обновление в JobSync: " : "JobSync: ") + r.message, { kind: failed ? "error" : "info", ms: 8000 });
      if (failed && r.log) console.warn(r.log);
      checked = new Set();
      await load();
      onchange();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    } finally {
      busy = false;
    }
  }

  function startEdit() {
    if (!job) return;
    draft = { title: title(job), company: company(job), description: description(job), notes: job.notes };
    editing = true;
    tick().then(() => document.getElementById("edit-title")?.focus());
  }

  async function saveEdit() {
    if (!job || !editing) return;
    editing = false;
    try {
      const j = await Panel.saveEdit(job.id, draft);
      jobs[cur] = j;
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }

  function toggleCheck(id: number) {
    const s = new Set(checked);
    s.has(id) ? s.delete(id) : s.add(id);
    checked = s;
  }

  function open() {
    if (job) Panel.openURL(job.url).catch((e) => toast(errText(e), { kind: "error" }));
  }

  function onKey(e: KeyboardEvent) {
    const t = e.target as HTMLElement;
    const typing = t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT";
    if (confirmSend) {
      if (e.key === "Escape") confirmSend = null;
      if (e.key === "Enter") { e.preventDefault(); doSend(); }
      return;
    }
    if (editing) {
      if (e.key === "Escape") { editing = false; e.preventDefault(); }
      if (e.key === "Enter" && e.metaKey) { e.preventDefault(); saveEdit(); }
      return;
    }
    if (typing) {
      if (e.key === "Escape" || (e.key === "Enter" && t === searchEl)) { t.blur(); e.preventDefault(); }
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    const map: Record<string, () => void> = {
      j: () => select(cur + 1), ArrowDown: () => select(cur + 1),
      k: () => select(cur - 1), ArrowUp: () => select(cur - 1),
      s: () => act("shortlisted"), d: () => act("dismissed"), Backspace: () => act("dismissed"),
      e: startEdit, o: open, u: undo, Enter: () => askSend(),
      "/": () => searchEl?.focus(), " ": () => job && toggleCheck(job.id),
      r: () => job?.status === "dismissed" && act("new"),
      Escape: () => (checked = new Set()),
      Home: () => select(0), End: () => select(jobs.length - 1),
    };
    if (map[k]) { e.preventDefault(); map[k](); }
  }

  onMount(() => {
    load();
    loadMeta();
    const offs = [events.jobsChanged.on(() => { load(); loadMeta(); })];
    const t = setInterval(loadMeta, 60000);
    return () => { offs.forEach((o) => o()); clearInterval(t); };
  });

  const statusTabs = [
    { v: "", label: "Входящие" }, { v: "new", label: "Новые" }, { v: "shortlisted", label: "Оставленные" },
    { v: "sent", label: "В JobSync" }, { v: "dismissed", label: "Корзина" },
  ];
</script>

<svelte:window onkeydown={onKey} />

<div class="inbox">
  <header class="toolbar">
    <div class="tabs">
      {#each statusTabs as t}
        <button class="tab" class:on={filter.status === t.v} onclick={() => setFilter({ status: t.v })}>{t.label}</button>
      {/each}
    </div>
    <div class="spacer"></div>
    {#if budget}
      <span class="budget" title={`Лимит JobSync — ${budget.hardLimit} вызовов в час на всё; скрипт тратит не больше ${budget.budget}`}>
        JobSync: осталось <b>{budget.remaining}</b> из {budget.budget}
        {#if budget.remaining < budget.budget && budget.resetInMinutes}<span class="muted"> · +через {budget.resetInMinutes} мин.</span>{/if}
      </span>
    {/if}
  </header>

  <div class="filters">
    <input type="search" placeholder="Поиск  /" bind:this={searchEl} value={filter.query}
      oninput={(e) => setFilter({ query: (e.target as HTMLInputElement).value })} />
    <select value={filter.source} onchange={(e) => setFilter({ source: (e.target as HTMLSelectElement).value })}>
      <option value="">Все источники</option>
      {#each sources as s}<option value={s}>{s}</option>{/each}
    </select>
    <select value={filter.market} onchange={(e) => setFilter({ market: (e.target as HTMLSelectElement).value })}>
      <option value="">РФ и мир</option><option value="world">Мир</option><option value="ru">РФ</option>
    </select>
    <select value={filter.lang} onchange={(e) => setFilter({ lang: (e.target as HTMLSelectElement).value })}>
      <option value="">Любой стек</option><option value="go">Go</option><option value="python">Python</option>
    </select>
    <label class="check"><input type="checkbox" checked={filter.remote}
      onchange={(e) => setFilter({ remote: (e.target as HTMLInputElement).checked })} /> remote</label>
    <select value={String(filter.minScore)} onchange={(e) => setFilter({ minScore: +(e.target as HTMLSelectElement).value })}>
      <option value="0">Оценка: любая</option><option value="5">от 5</option><option value="6">от 6</option>
      <option value="7">от 7</option><option value="8">от 8</option>
    </select>
    <select value={filter.sort} onchange={(e) => setFilter({ sort: (e.target as HTMLSelectElement).value })}>
      <option value="score">По оценке</option><option value="date">По дате</option>
      <option value="new">Новые в панели</option><option value="company">По компании</option>
    </select>
  </div>

  <div class="split">
    <section class="list-pane">
      {#if checked.size}
        <div class="batch">
          <span>Выбрано: <b>{checked.size}</b></span>
          <button onclick={() => act("shortlisted")}>★ Оставить</button>
          <button onclick={() => askSend()}>Отправить</button>
          <button class="danger" onclick={() => act("dismissed")}>Удалить</button>
          <button class="ghost" onclick={() => (checked = new Set())}>Снять <kbd>esc</kbd></button>
        </div>
      {:else}
        <div class="batch muted small">{total} {total === 1 ? "вакансия" : "вакансий"} · <kbd>j</kbd><kbd>k</kbd> навигация · <kbd>␣</kbd> выбрать</div>
      {/if}
      <div class="list" bind:this={listEl} bind:clientHeight={viewH} onscroll={(e) => (scrollTop = (e.target as HTMLElement).scrollTop)}>
        {#if loading && !jobs.length}
          <div class="empty"><span class="spinner"></span></div>
        {:else if !jobs.length}
          <div class="empty">
            <div class="big">📭</div>
            {#if filter.query || filter.source || filter.market || filter.lang || filter.remote || filter.minScore}
              <h3>Ничего не нашлось</h3><span>Ослабьте фильтры или очистите поиск.</span>
            {:else if filter.status === "dismissed"}
              <h3>Корзина пуста</h3>
            {:else}
              <h3>Новых вакансий нет</h3><span>Запустите поиск во вкладке «Запуски» — результаты появятся здесь.</span>
            {/if}
          </div>
        {:else}
          <div class="spacer-rows" style:height="{jobs.length * ROW}px">
            {#each jobs.slice(start, end) as j, k (j.id)}
              {@const i = start + k}
              <!-- svelte-ignore a11y_click_events_have_key_events -->
              <div class="row" class:cur={i === cur} class:checked={checked.has(j.id)} style:top="{i * ROW}px"
                role="option" tabindex="-1" aria-selected={i === cur} onclick={() => select(i)} ondblclick={open}>
                <span class="bar" style:background={scoreColor(j.score)} style:opacity={j.score == null ? 0.35 : 1}></span>
                <input type="checkbox" class="pick" checked={checked.has(j.id)} onclick={(e) => { e.stopPropagation(); toggleCheck(j.id); }} />
                <div class="row-main">
                  <div class="row-top">
                    <span class="r-title">{title(j)}</span>
                    {#if j.status !== "new"}<span class="dot {statusMeta[j.status]?.cls}" title={statusMeta[j.status]?.label}>{statusMeta[j.status]?.icon}</span>{/if}
                    {#if j.queued}<span class="dot st-short" title="В очереди на отправку">⏳</span>{/if}
                  </div>
                  <div class="row-sub">
                    <span class="r-company">{company(j)}</span>
                    <span class="chip src">{j.source}</span>
                    <span class="r-meta">{region(j)}{j.salary ? ` · ${j.salary}` : ""}</span>
                  </div>
                </div>
                <div class="row-right">
                  {#if j.score != null}<span class="score" style:color={scoreColor(j.score)}>{j.score}</span>{/if}
                  <span class="age">{shortAge(j.postedAt || j.firstSeen)}</span>
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    </section>

    <section class="card-pane">
      {#if job}
        <article class="card detail">
          {#if editing}
            <div class="edit">
              <label>Название<input id="edit-title" type="text" bind:value={draft.title} /></label>
              <label>Компания<input type="text" bind:value={draft.company} /></label>
              <label class="grow">Описание<textarea bind:value={draft.description} rows="16"></textarea></label>
              <label>Заметки<textarea bind:value={draft.notes} rows="3" placeholder="Только для вас, в JobSync не уходят"></textarea></label>
              <div class="actions">
                <span class="muted small">Оригинал не затирается: правки хранятся отдельно и уходят в JobSync.</span>
                <div class="spacer"></div>
                <button class="ghost" onclick={() => (editing = false)}>Отмена <kbd>esc</kbd></button>
                <button class="primary" onclick={saveEdit}>Сохранить <kbd>⌘↵</kbd></button>
              </div>
            </div>
          {:else}
            <header class="d-head">
              <div class="chips">
                <span class="chip {statusMeta[job.status]?.cls}">{statusMeta[job.status]?.icon} {statusMeta[job.status]?.label}</span>
                {#if job.queued}<span class="chip warn">⏳ в очереди на отправку</span>{/if}
                <span class="chip src">{job.source}</span>
                {#if job.market === "ru"}<span class="chip">РФ</span>{/if}
                {#if job.go}<span class="chip">Go</span>{/if}
                {#if job.python}<span class="chip">Python</span>{/if}
                {#if job.years}<span class="chip">опыт от {job.years} лет</span>{/if}
              </div>
              <h1 class="selectable">{title(job)}{#if job.editTitle}<span class="edited" title={`Оригинал: ${job.title}`}>✎</span>{/if}</h1>
              <div class="sub selectable">
                <b>{company(job)}</b>{#if job.editCompany}<span class="edited" title={`Оригинал: ${job.company}`}>✎</span>{/if}
                {#if region(job)}<span class="muted"> · {region(job)}</span>{/if}
                {#if job.salary}<span> · {job.salary}</span>{/if}
              </div>
              <div class="meta muted small">
                {#if job.postedAt}опубликовано {ago(job.postedAt)} · {/if}в панели {ago(job.firstSeen)}
                {#if job.sentAt} · в JobSync {ago(job.sentAt)}{/if}
                · <button class="link" onclick={open}>открыть оригинал ↗</button>
              </div>
            </header>

            <div class="d-body selectable">
              {#if job.score != null || job.ai || job.flags.length}
                <div class="why">
                  {#if job.score != null}
                    <div class="score-line">
                      <span class="meter"><span style:width="{job.score * 10}%" style:background={scoreColor(job.score)}></span></span>
                      <b style:color={scoreColor(job.score)}>{job.score}/10</b>
                      <span class="muted small">{job.scoreKind === "ai" ? "оценка AI" : "эвристика скрипта"}{job.ai?.level ? ` · уровень: ${job.ai.level}` : ""}</span>
                    </div>
                  {/if}
                  {#if job.ai?.reason}<p class="reason">✓ {job.ai.reason}</p>{/if}
                  {#if job.ai?.redFlags?.length || job.flags.length}
                    <ul class="flags">
                      {#each job.ai?.redFlags ?? [] as f}<li class="bad">⚠︎ {f}</li>{/each}
                      {#each job.flags as f}<li class:bad={f.startsWith("РФ") || f.startsWith("ограничение")}>• {f}</li>{/each}
                    </ul>
                  {/if}
                </div>
              {/if}

              {#if job.notes}<div class="notes"><span class="muted small">Заметки</span><p>{job.notes}</p></div>{/if}

              {#if description(job)}
                <div class="desc">
                  {#each blocks(description(job)) as b}
                    {#if b.kind === "p"}<p>{b.text}</p>{:else}<ul>{#each b.items as it}<li>{it}</li>{/each}</ul>{/if}
                  {/each}
                </div>
              {:else}
                <p class="muted">Описания нет — откройте оригинал (<kbd>o</kbd>) или добавьте своё (<kbd>e</kbd>).</p>
              {/if}
            </div>

            <footer class="d-foot">
              {#if job.status === "dismissed"}
                <button class="primary" onclick={() => act("new")}>Вернуть <kbd>r</kbd></button>
              {:else}
                <button onclick={() => act("shortlisted")} disabled={job.status === "shortlisted" || job.status === "sent"}>★ Оставить <kbd>s</kbd></button>
                {#if job.status === "sent"}
                  <button onclick={() => askSend(true)}>Обновить в JobSync</button>
                {:else}
                  <button class="primary" onclick={() => askSend()} disabled={busy}>Отправить в JobSync <kbd>↵</kbd></button>
                {/if}
                <button onclick={startEdit}>Изменить <kbd>e</kbd></button>
                <div class="spacer"></div>
                <button class="danger" onclick={() => act("dismissed")}>Удалить <kbd>d</kbd></button>
              {/if}
            </footer>
          {/if}
        </article>
      {:else if !loading}
        <div class="empty"><span>Выберите вакансию слева</span></div>
      {/if}
    </section>
  </div>
</div>

{#if confirmSend}
  {@const cost = confirmSend.resend ? confirmSend.ids.length : confirmSend.ids.length * 2}
  <div class="modal-bg" role="presentation" onclick={() => (confirmSend = null)}>
    <div class="modal card" role="dialog" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <h3>{confirmSend.resend ? "Обновить в JobSync" : "Отправить в JobSync"}: {confirmSend.ids.length}</h3>
      <p class="muted">
        {#if confirmSend.resend}
          Обновит уже отправленные записи по ссылке (upsert), дублей не будет. Около {cost} вызовов лимита.
        {:else}
          С проверкой дублей (find_job) — около {cost} вызовов лимита.
        {/if}
        {#if budget}Сейчас доступно {budget.remaining} из {budget.budget}.{/if}
        {#if budget && cost > budget.remaining} Остальное встанет в очередь и уйдёт, когда лимит освободится.{/if}
      </p>
      <div class="actions"><div class="spacer"></div>
        <button class="ghost" onclick={() => (confirmSend = null)}>Отмена <kbd>esc</kbd></button>
        <button class="primary" onclick={doSend}>{confirmSend.resend ? "Обновить" : "Отправить"} <kbd>↵</kbd></button>
      </div>
    </div>
  </div>
{/if}

<style>
  .inbox { display: flex; flex-direction: column; height: 100%; }
  .toolbar { height: 44px; display: flex; align-items: center; gap: 10px; padding: 0 16px; --app-region: drag; flex: none; }
  .toolbar button, .toolbar .budget { --app-region: no-drag; }
  .tabs { display: flex; gap: 2px; background: var(--hover); padding: 2px; border-radius: 8px; }
  .tab { background: transparent; padding: 3px 11px; border-radius: 6px; font-size: 12px; }
  .tab.on { background: var(--bg); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.12); font-weight: 500; }
  .spacer { flex: 1; }
  .budget { font-size: 12px; color: var(--muted); }
  .budget b { color: var(--text); font-variant-numeric: tabular-nums; }
  .filters { display: flex; gap: 8px; padding: 0 16px 10px; align-items: center; flex-wrap: wrap; }
  .filters input[type="search"] { width: 220px; }
  .filters select { padding: 4px 6px; }
  .check { display: flex; gap: 5px; align-items: center; font-size: 12px; color: var(--muted); }

  .split { flex: 1; display: grid; grid-template-columns: minmax(330px, 40%) 1fr; gap: 12px; padding: 0 12px 12px; min-height: 0; }
  .list-pane { display: flex; flex-direction: column; min-height: 0; background: var(--bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
  .batch { display: flex; gap: 6px; align-items: center; padding: 8px 10px; border-bottom: 1px solid var(--line); min-height: 41px; flex-wrap: wrap; }
  .batch button { padding: 3px 9px; font-size: 12px; }
  .list { flex: 1; overflow-y: auto; position: relative; outline: none; }
  .spacer-rows { position: relative; }
  .row {
    position: absolute; left: 0; right: 0; height: 46px;
    display: flex; align-items: center; gap: 8px; padding: 0 12px 0 0;
    border-bottom: 1px solid var(--line);
  }
  .row:hover { background: var(--hover); }
  .row.cur { background: var(--sel); }
  .row.checked { box-shadow: inset 3px 0 0 var(--accent); }
  .bar { width: 4px; align-self: stretch; margin: 7px 0; border-radius: 0 3px 3px 0; flex: none; }
  .pick { margin: 0; opacity: 0.35; flex: none; }
  .row:hover .pick, .row.checked .pick { opacity: 1; }
  .row-main { flex: 1; min-width: 0; }
  .row-top { display: flex; align-items: center; gap: 6px; }
  .r-title { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row-sub { display: flex; gap: 6px; align-items: center; font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; }
  .r-company { color: var(--text); opacity: 0.85; overflow: hidden; text-overflow: ellipsis; max-width: 45%; }
  .r-meta { overflow: hidden; text-overflow: ellipsis; }
  .row-sub .chip { padding: 0 6px; font-size: 10.5px; }
  .dot { font-size: 11px; }
  .dot.st-short { color: var(--amber); } .dot.st-sent { color: var(--green); } .dot.st-dism { color: var(--red); }
  .row-right { display: flex; flex-direction: column; align-items: flex-end; gap: 1px; flex: none; }
  .score { font-weight: 600; font-variant-numeric: tabular-nums; font-size: 13px; }
  .age { font-size: 11px; color: var(--muted-2); }

  .card-pane { min-height: 0; display: flex; }
  .detail { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .d-head { padding: 18px 22px 12px; border-bottom: 1px solid var(--line); }
  .chips { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 8px; }
  h1 { font-size: 19px; line-height: 1.3; margin: 0 0 4px; font-weight: 650; }
  .sub { font-size: 13.5px; }
  .edited { color: var(--accent); font-size: 12px; margin-left: 5px; cursor: help; }
  .meta { margin-top: 6px; }
  .link { background: none; padding: 0; color: var(--accent); }
  .link:hover:not(:disabled) { background: none; text-decoration: underline; }
  .d-body { flex: 1; overflow-y: auto; padding: 14px 22px 20px; }
  .why { background: var(--bg-2); border-radius: 8px; padding: 10px 12px; margin-bottom: 14px; }
  .score-line { display: flex; align-items: center; gap: 10px; }
  .meter { width: 120px; height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; }
  .meter span { display: block; height: 100%; border-radius: 3px; }
  .reason { margin: 8px 0 0; }
  .flags { margin: 6px 0 0; padding: 0; list-style: none; display: grid; gap: 2px; font-size: 12.5px; color: var(--muted); }
  .flags .bad { color: var(--amber); }
  .notes { border-left: 3px solid var(--amber); padding: 2px 10px; margin-bottom: 14px; }
  .notes p { margin: 2px 0 0; white-space: pre-wrap; }
  .desc { font-size: 13.5px; line-height: 1.6; max-width: 760px; }
  .desc p { margin: 0 0 9px; }
  .desc ul { margin: 0 0 10px; padding-left: 20px; }
  .desc li { margin: 2px 0; }
  .d-foot { display: flex; gap: 8px; align-items: center; padding: 10px 14px; border-top: 1px solid var(--line); flex-wrap: wrap; }

  .edit { display: flex; flex-direction: column; gap: 10px; padding: 18px 22px; height: 100%; overflow-y: auto; }
  .edit label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
  .edit .grow { flex: 1; }
  .edit .grow textarea { flex: 1; min-height: 220px; }
  .actions { display: flex; gap: 8px; align-items: center; }

  .modal-bg { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.25); display: grid; place-items: center; z-index: 40; }
  .modal { width: 440px; padding: 18px 20px; }
  .modal h3 { margin: 0 0 6px; font-size: 15px; }
</style>
