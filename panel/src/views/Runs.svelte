<script lang="ts">
  import { onMount, tick } from "svelte";
  import { Panel, events, type Run, type Settings } from "../mygo";
  import { ago, errText, fmtDate } from "../lib/util";
  import { toast } from "../lib/toast.svelte";

  let runs = $state<Run[]>([]);
  let running = $state<string[]>([]);
  let settings = $state<Settings | null>(null);
  let shown = $state<Run | null>(null); // the run whose log is shown
  let live = $state<Record<string, string[]>>({ boards: [], ru: [] });
  let liveScript = $state<string | null>(null);
  let logEl = $state<HTMLPreElement>();
  let follow = $state(true);

  async function load() {
    try {
      runs = await Panel.runs();
      running = await Panel.running();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }

  onMount(() => {
    load();
    Panel.settings().then((s) => (settings = s)).catch(() => {});
    const offs = [
      events.runLine.on((l) => {
        const arr = live[l.script] ?? [];
        arr.push(l.text);
        if (arr.length > 5000) arr.splice(0, arr.length - 5000);
        live[l.script] = arr;
        if (!running.includes(l.script)) running = [...running, l.script];
        if (liveScript === l.script && follow) tick().then(() => logEl && (logEl.scrollTop = logEl.scrollHeight));
      }),
      events.runDone.on(async (r) => {
        await load();
        if (liveScript === r.script) shown = await Panel.runLog(r.id).catch(() => r);
        liveScript = liveScript === r.script ? null : liveScript;
      }),
    ];
    return () => offs.forEach((o) => o());
  });

  async function start(script: string) {
    try {
      live[script] = [];
      liveScript = script;
      shown = null;
      follow = true;
      await Panel.startRun(script);
      running = [...running, script];
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  async function cancel(script: string) {
    await Panel.cancelRun(script);
    toast(`${script}: останавливаю…`);
  }
  async function showRun(r: Run) {
    liveScript = null;
    try {
      shown = await Panel.runLog(r.id);
      await tick();
      if (logEl) logEl.scrollTop = logEl.scrollHeight;
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  function watchLive(script: string) {
    shown = null;
    liveScript = script;
    follow = true;
  }

  const scripts = [
    { id: "boards", name: "Поиск бордов", desc: "Greenhouse / Lever / Ashby, HN и открытые API — зарубежный рынок", key: "argsBoards" as const },
    { id: "ru", name: "Поиск RU", desc: "Хабр Карьера, Telegram, Яндекс, VK, МТС, «Работа России»", key: "argsRu" as const },
  ];
  const runStatus: Record<string, { t: string; cls: string }> = {
    ok: { t: "✓ успех", cls: "st-sent" }, failed: { t: "✕ ошибка", cls: "st-dism" },
    canceled: { t: "■ остановлен", cls: "" }, running: { t: "… идёт", cls: "st-new" },
  };
  const logText = $derived(liveScript ? (live[liveScript] ?? []).join("\n") : (shown?.log ?? ""));
</script>

<div class="page">
  <header class="head"><h2>Запуски</h2></header>
  <div class="starters">
    {#each scripts as s}
      {@const isRunning = running.includes(s.id)}
      <div class="card starter">
        <div class="s-top">
          <div>
            <h3>{s.name}</h3>
            <p class="muted small">{s.desc}</p>
          </div>
          {#if isRunning}
            <div class="btns">
              <button onclick={() => watchLive(s.id)}><span class="spinner"></span> журнал</button>
              <button class="danger" onclick={() => cancel(s.id)}>Остановить</button>
            </div>
          {:else}
            <button class="primary" onclick={() => start(s.id)}>Запустить</button>
          {/if}
        </div>
        <code class="mono cmd">python3 {s.id === "boards" ? "boards/find_boards.py" : "ru/find_ru_jobs.py"} {settings?.[s.key] ?? ""}</code>
      </div>
    {/each}
  </div>

  <div class="bottom">
    <section class="card hist">
      <h3 class="sec">История</h3>
      {#if !runs.length}
        <p class="muted pad">Пока пусто. После успешного запуска новые вакансии сами появятся во «Входящих».</p>
      {/if}
      <div class="hist-list">
        {#each runs as r}
          <button class="hrow" class:on={shown?.id === r.id} onclick={() => showRun(r)}>
            <span class="h-name">{r.script === "boards" ? "Борды" : "RU"}</span>
            <span class="chip {runStatus[r.status]?.cls}">{runStatus[r.status]?.t ?? r.status}</span>
            <span class="muted small h-time" title={fmtDate(r.started)}>{ago(r.started)}</span>
            <span class="small">{r.status === "ok" ? `+${r.imported}` : ""}</span>
          </button>
        {/each}
      </div>
    </section>
    <section class="card logbox">
      <div class="log-head">
        {#if liveScript}
          <b>Живой журнал: {liveScript}</b>
          <label class="muted small"><input type="checkbox" bind:checked={follow} /> прокручивать</label>
        {:else if shown}
          <b>{shown.script} · {fmtDate(shown.started)}</b>
          <span class="muted small">код выхода {shown.exitCode ?? "—"} · аргументы: <span class="mono">{shown.args || "по умолчанию"}</span></span>
        {:else}
          <span class="muted">Выберите запуск или запустите скрипт</span>
        {/if}
      </div>
      <pre class="log selectable" bind:this={logEl}>{logText}</pre>
    </section>
  </div>
</div>

<style>
  .page { height: 100%; display: flex; flex-direction: column; padding: 0 20px 20px; }
  .head { height: 44px; display: flex; align-items: center; --app-region: drag; flex: none; }
  h2 { font-size: 15px; margin: 0; }
  .starters { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; flex: none; }
  .starter { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .s-top { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  h3 { margin: 0; font-size: 14px; }
  .starter p { margin: 2px 0 0; }
  .btns { display: flex; gap: 6px; }
  .cmd { color: var(--muted); background: var(--bg-2); padding: 5px 8px; border-radius: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bottom { flex: 1; display: grid; grid-template-columns: 270px 1fr; gap: 12px; margin-top: 12px; min-height: 0; }
  .hist { display: flex; flex-direction: column; min-height: 0; }
  .sec { font-size: 13px; padding: 12px 14px 6px; }
  .pad { padding: 0 14px; }
  .hist-list { overflow-y: auto; padding: 0 6px 6px; }
  .hrow { display: grid; grid-template-columns: 50px auto 1fr auto; gap: 6px; align-items: center; width: 100%; background: transparent; text-align: left; padding: 7px 8px; }
  .hrow.on { background: var(--sel); }
  .h-time { text-align: right; }
  .logbox { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .log-head { display: flex; gap: 12px; align-items: center; padding: 10px 14px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .log { flex: 1; margin: 0; overflow: auto; padding: 10px 14px; font: 11.5px/1.5 var(--mono); white-space: pre-wrap; word-break: break-word; }
</style>
