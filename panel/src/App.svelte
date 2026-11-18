<script lang="ts">
  import { onMount } from "svelte";
  import { Panel, events, type Counts, type MainStatus } from "./mygo";
  import Inbox from "./views/Inbox.svelte";
  import Overview from "./views/Overview.svelte";
  import Runs from "./views/Runs.svelte";
  import Settings from "./views/Settings.svelte";
  import { toasts, dismiss, toast } from "./lib/toast.svelte";
  import { errText } from "./lib/util";

  type View = "inbox" | "overview" | "runs" | "settings";
  let view = $state<View>("inbox");
  let status = $state<MainStatus | null>(null);
  let counts = $state<Counts | null>(null);
  let running = $state<string[]>([]);

  let started = false;
  async function refresh() {
    try {
      status = await Panel.status();
      if (!started) {
        started = true;
        const v = status.startView as View | undefined;
        if (v && ["inbox", "overview", "runs", "settings"].includes(v)) view = v;
      }
      if (status.ready) {
        counts = await Panel.counts();
        running = await Panel.running();
      } else {
        view = "settings";
      }
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }

  onMount(() => {
    refresh();
    const offs = [
      events.jobsChanged.on(refresh),
      events.runDone.on((r) => {
        refresh();
        if (r.status === "ok") toast(`${r.script}: готово, новых вакансий ${r.imported}`);
        else if (r.status === "failed") toast(`${r.script}: ошибка — откройте журнал в «Запусках»`, { kind: "error" });
      }),
    ];
    const t = setInterval(refresh, 30000);
    const keys = (e: KeyboardEvent) => {
      if (!e.metaKey) return;
      const v = ({ "1": "inbox", "2": "overview", "3": "runs", "4": "settings" } as Record<string, View>)[e.key];
      if (v) {
        e.preventDefault();
        view = v;
      }
    };
    window.addEventListener("keydown", keys);
    return () => {
      offs.forEach((o) => o());
      clearInterval(t);
      window.removeEventListener("keydown", keys);
    };
  });

  const nav: { id: View; label: string; icon: string; key: string }[] = [
    { id: "inbox", label: "Входящие", icon: "tray", key: "1" },
    { id: "overview", label: "Сводка", icon: "chart", key: "2" },
    { id: "runs", label: "Запуски", icon: "play", key: "3" },
    { id: "settings", label: "Настройки", icon: "gear", key: "4" },
  ];
</script>

<div class="shell">
  <aside class="sidebar">
    <div class="drag"></div>
    <div class="brand">Панель вакансий</div>
    <nav>
      {#each nav as n}
        <button class="nav" class:active={view === n.id} onclick={() => (view = n.id)} title={`⌘${n.key}`}
          disabled={!status?.ready && n.id !== "settings"}>
          <svg viewBox="0 0 16 16" class="ico" aria-hidden="true">
            {#if n.icon === "tray"}
              <path d="M2 9.5V13a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V9.5M2 9.5 4 3h8l2 6.5M2 9.5h3.5l1 1.5h3l1-1.5H14" />
            {:else if n.icon === "chart"}
              <path d="M2.5 13.5h11M4.5 11V7.5M8 11V4M11.5 11V6" />
            {:else if n.icon === "play"}
              <path d="M5 3.5v9l7-4.5z" />
            {:else}
              <circle cx="8" cy="8" r="2.2" /><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.6 3.6l1.4 1.4M11 11l1.4 1.4M3.6 12.4 5 11M11 5l1.4-1.4" />
            {/if}
          </svg>
          <span>{n.label}</span>
          {#if n.id === "inbox" && counts?.new}<span class="count">{counts.new}</span>{/if}
          {#if n.id === "runs" && running.length}<span class="spinner"></span>{/if}
        </button>
      {/each}
    </nav>
    <div class="side-foot">
      {#if counts}
        <div class="stat"><span>Оставлено</span><b>{counts.shortlisted}</b></div>
        <div class="stat"><span>В JobSync</span><b>{counts.sent}</b></div>
        {#if counts.queued}<div class="stat"><span>В очереди</span><b>{counts.queued}</b></div>{/if}
      {/if}
    </div>
  </aside>

  <main class="content">
    {#if !status}
      <div class="empty"><span class="spinner"></span></div>
    {:else if view === "inbox"}
      <Inbox onchange={refresh} />
    {:else if view === "overview"}
      <Overview goto={(v: View) => (view = v)} />
    {:else if view === "runs"}
      <Runs />
    {:else}
      <Settings {status} onchange={refresh} />
    {/if}
  </main>
</div>

<div class="toasts">
  {#each toasts as t (t.id)}
    <div class="toast" class:error={t.kind === "error"}>
      <span>{t.text}</span>
      {#if t.action}
        <button class="ghost link" onclick={() => { t.action!.run(); dismiss(t.id); }}>{t.action.label}</button>
      {/if}
      <button class="ghost x" onclick={() => dismiss(t.id)} aria-label="Закрыть">✕</button>
    </div>
  {/each}
</div>

<style>
  .shell { display: grid; grid-template-columns: 210px 1fr; height: 100%; }
  .sidebar {
    background: var(--sidebar);
    display: flex;
    flex-direction: column;
    padding: 0 10px 12px;
    border-right: 1px solid var(--line);
  }
  .drag { height: 44px; --app-region: drag; flex: none; }
  .brand { font-weight: 600; font-size: 12px; color: var(--muted); padding: 0 10px 8px; }
  nav { display: flex; flex-direction: column; gap: 2px; }
  .nav {
    display: flex; align-items: center; gap: 9px;
    background: transparent; padding: 6px 10px; border-radius: 7px; text-align: left; font-size: 13px;
  }
  .nav:hover:not(:disabled) { background: var(--hover); }
  .nav.active { background: rgba(127, 127, 127, 0.2); }
  .ico { width: 16px; height: 16px; fill: none; stroke: var(--accent); stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; flex: none; }
  .nav span:nth-of-type(1) { flex: 1; }
  .count { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .side-foot { margin-top: auto; padding: 8px 10px; display: grid; gap: 4px; }
  .stat { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
  .stat b { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
  .content { background: var(--bg-2); min-width: 0; height: 100%; overflow: hidden; }

  .toasts { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); display: grid; gap: 8px; z-index: 50; }
  .toast {
    display: flex; align-items: center; gap: 10px;
    background: color-mix(in srgb, var(--text) 88%, transparent); color: var(--bg);
    padding: 8px 8px 8px 14px; border-radius: 10px; box-shadow: var(--shadow); font-size: 13px;
    backdrop-filter: blur(12px); max-width: 640px;
  }
  .toast.error { background: color-mix(in srgb, var(--red) 92%, black); color: white; }
  .toast .link { color: inherit; font-weight: 600; text-decoration: underline; padding: 2px 6px; }
  .toast .x { color: inherit; opacity: 0.6; padding: 2px 6px; }
</style>
