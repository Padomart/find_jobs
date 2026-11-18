<script lang="ts">
  import { onMount } from "svelte";
  import { Panel, events, type Summary } from "../mygo";
  import { ago, errText } from "../lib/util";
  import { toast } from "../lib/toast.svelte";

  let { goto }: { goto: (v: "inbox" | "runs") => void } = $props();
  let s = $state<Summary | null>(null);

  async function load() {
    try {
      s = await Panel.summary();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  onMount(() => {
    load();
    const offs = [events.runDone.on(load), events.jobsChanged.on(load)];
    return () => offs.forEach((o) => o());
  });

  const scriptName: Record<string, string> = { boards: "Борды (зарубеж)", ru: "RU" };
  const runStatus: Record<string, { t: string; cls: string }> = {
    ok: { t: "✓ успех", cls: "st-sent" }, failed: { t: "✕ ошибка", cls: "st-dism" },
    canceled: { t: "■ остановлен", cls: "" }, running: { t: "… идёт", cls: "st-new" },
  };
  function dur(a: string, b: string) {
    if (!a || !b) return "";
    const s = Math.round((Date.parse(b) - Date.parse(a)) / 1000);
    return s < 60 ? `${s} с` : `${Math.floor(s / 60)} мин ${s % 60} с`;
  }
</script>

<div class="page">
  <header class="head"><h2>Сводка</h2></header>
  {#if !s}
    <div class="empty"><span class="spinner"></span></div>
  {:else}
    <div class="grid">
      <button class="card tile" onclick={() => goto("inbox")}>
        <span class="n" style:color="var(--accent)">{s.counts.new}</span><span class="l">● новых</span>
      </button>
      <button class="card tile" onclick={() => goto("inbox")}>
        <span class="n" style:color="var(--amber)">{s.counts.shortlisted}</span><span class="l">★ на проверке</span>
      </button>
      <div class="card tile"><span class="n" style:color="var(--green)">{s.counts.sent}</span><span class="l">✓ в JobSync</span></div>
      <div class="card tile">
        <span class="n">{s.budget.remaining}<small>/{s.budget.budget}</small></span>
        <span class="l">лимит JobSync на час{s.counts.queued ? ` · в очереди ${s.counts.queued}` : ""}</span>
        <span class="meter"><span style:width="{(s.budget.remaining / s.budget.budget) * 100}%"></span></span>
      </div>
    </div>

    <section class="card block">
      <div class="block-head"><h3>Последние запуски</h3><button class="ghost" onclick={() => goto("runs")}>Все запуски →</button></div>
      {#if !s.runs.length}
        <p class="muted pad">Запусков ещё не было. Откройте «Запуски» и запустите поиск.</p>
      {:else}
        <table>
          <tbody>
            {#each s.runs as r}
              <tr>
                <td>{scriptName[r.script] ?? r.script}</td>
                <td><span class="chip {runStatus[r.status]?.cls}">{runStatus[r.status]?.t ?? r.status}</span></td>
                <td class="muted">{ago(r.started)}</td>
                <td class="muted">{dur(r.started, r.finished)}</td>
                <td>{r.status === "ok" ? `новых ${r.imported}` : ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>

    <section class="card block">
      <div class="block-head"><h3>Здоровье источников</h3><span class="muted small">по журналу последнего запуска каждого скрипта</span></div>
      {#if !s.health.length}
        <p class="muted pad">Появится после первого запуска скриптов из панели.</p>
      {:else}
        <table>
          <thead><tr><th>Источник</th><th></th><th class="num">Получено</th><th class="num">Прошло</th><th>Состояние</th></tr></thead>
          <tbody>
            {#each s.health as h}
              <tr class:warn={!h.ok || h.passed === 0}>
                <td><b>{h.source}</b></td>
                <td class="muted small">{scriptName[h.script]}</td>
                <td class="num">{h.got || ""}</td>
                <td class="num">{h.ok ? h.passed : ""}</td>
                <td>
                  {#if !h.ok}<span class="chip bad">✕</span>{:else if h.passed === 0}<span class="chip warn">0</span>{:else}<span class="chip st-sent">✓</span>{/if}
                  <span class="muted small">{h.note}</span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>
  {/if}
</div>

<style>
  .page { height: 100%; overflow-y: auto; padding: 0 20px 24px; }
  .head { height: 44px; display: flex; align-items: center; --app-region: drag; }
  h2 { font-size: 15px; margin: 0; }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px; }
  .tile { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; padding: 14px 16px; text-align: left; background: var(--bg); }
  button.tile:hover { background: var(--bg); box-shadow: var(--shadow), 0 0 0 2px var(--sel); }
  .n { font-size: 28px; font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.1; }
  .n small { font-size: 15px; color: var(--muted); font-weight: 500; }
  .l { color: var(--muted); font-size: 12px; }
  .meter { margin-top: 6px; width: 100%; height: 5px; border-radius: 3px; background: var(--line); overflow: hidden; }
  .meter span { display: block; height: 100%; background: var(--green); }
  .block { padding: 4px 0 6px; margin-bottom: 14px; }
  .block-head { display: flex; align-items: center; gap: 10px; padding: 10px 16px 6px; }
  .block-head h3 { margin: 0; font-size: 13px; flex: 1; }
  .pad { padding: 4px 16px 10px; margin: 0; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-weight: 500; color: var(--muted); font-size: 11.5px; padding: 4px 16px; }
  td { padding: 7px 16px; border-top: 1px solid var(--line); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.warn td:first-child b { color: var(--amber); }
</style>
