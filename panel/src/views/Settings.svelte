<script lang="ts">
  import { onMount } from "svelte";
  import { Panel, type ImportResult, type MainStatus, type Settings } from "../mygo";
  import { errText } from "../lib/util";
  import { toast } from "../lib/toast.svelte";

  let { status, onchange }: { status: MainStatus; onchange: () => void } = $props();
  let s = $state<Settings | null>(null);
  let rootInput = $state("");
  let blacklist = $state("");
  let imported = $state<ImportResult[] | null>(null);
  let confirmTrash = $state(false);

  async function load() {
    rootInput = status.root;
    if (!status.ready) return;
    try {
      s = await Panel.settings();
      blacklist = s.blacklist;
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  onMount(load);

  async function setRoot(path?: string) {
    try {
      if (path === undefined) {
        const p = await Panel.chooseRoot();
        if (!p) return;
        rootInput = p;
      } else {
        await Panel.setRoot(path);
      }
      toast("Проект открыт");
      onchange();
      status = await Panel.status();
      await load();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  async function save() {
    if (!s) return;
    try {
      await Panel.saveSettings(s);
      s = await Panel.settings();
      toast("Настройки сохранены");
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  async function saveBl() {
    try {
      await Panel.saveBlacklist(blacklist);
      toast("boards/blacklist.txt сохранён");
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  async function reimport() {
    try {
      imported = await Panel.import();
      onchange();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  async function emptyTrash() {
    confirmTrash = false;
    try {
      const n = await Panel.emptyTrash();
      toast(`Удалено навсегда: ${n}`);
      onchange();
    } catch (e) {
      toast(errText(e), { kind: "error" });
    }
  }
  const envHelp: Record<string, string> = {
    JOBSYNC_URL: "адрес JobSync — для отправки",
    JOBSYNC_TOKEN: "токен MCP из Settings → MCP Access",
    DEEPSEEK_API_KEY: "AI-оценка (--ai)",
    SUPERJOB_KEY: "источник SuperJob в RU-поиске",
  };
</script>

<div class="page">
  <header class="head"><h2>Настройки</h2></header>

  <section class="card block">
    <h3>Проект</h3>
    {#if status.error}<p class="err">{status.error}</p>{/if}
    <div class="row">
      <input type="text" class="grow mono" bind:value={rootInput} placeholder="/Users/…/find_jobs" />
      <button onclick={() => setRoot(rootInput)}>Открыть</button>
      <button onclick={() => setRoot()}>Выбрать папку…</button>
    </div>
    <p class="muted small">База панели: <span class="mono">state/panel/panel.db</span> внутри проекта (в .gitignore).</p>
  </section>

  {#if s}
    <section class="card block">
      <h3>Окружение</h3>
      <p class="muted small">Секреты читаются из <span class="mono">.env</span> и передаются только скриптам; здесь видно лишь, заданы ли они.
        {#if !s.envFile}<b class="warn">Файла .env нет — скопируйте .env.example в .env.</b>{/if}</p>
      <div class="env">
        {#each s.env as v}
          <div class="env-row">
            <span class="chip {v.set ? 'st-sent' : 'bad'}">{v.set ? "✓ задан" : "✕ не задан"}</span>
            <span class="mono">{v.name}</span><span class="muted small">{envHelp[v.name] ?? ""}</span>
          </div>
        {/each}
      </div>
      <div class="versions muted small">
        <span>Go: <b>{s.versions.go}</b></span>
        <span>Python: <b>{s.versions.python}</b> <span class="mono">({s.versions.pythonPath})</span></span>
      </div>
    </section>

    <section class="card block">
      <h3>Скрипты и отправка</h3>
      <label>Флаги boards/find_boards.py
        <div class="row"><input type="text" class="grow mono" bind:value={s.argsBoards} />
          <button class="ghost" onclick={() => (s!.argsBoards = s!.defaultBoards)}>Из README</button></div>
      </label>
      <label>Флаги ru/find_ru_jobs.py
        <div class="row"><input type="text" class="grow mono" bind:value={s.argsRu} placeholder="без флагов — все включённые источники" />
          <button class="ghost" onclick={() => (s!.argsRu = s!.defaultRu)}>Из README</button></div>
      </label>
      <label>Python (пусто — найти автоматически)
        <input type="text" class="mono" bind:value={s.python} />
      </label>
      <label class="inline"><input type="checkbox" bind:checked={s.checkExisting} />
        Перед отправкой проверять дубль в JobSync (find_job, +1 вызов лимита на вакансию)</label>
      <div class="row end"><button class="primary" onclick={save}>Сохранить</button></div>
    </section>

    <section class="card block">
      <h3>Чёрный список компаний</h3>
      <p class="muted small">boards/blacklist.txt — по одной компании (или ссылке на борд) в строке, # — комментарий.</p>
      <textarea class="mono" rows="9" bind:value={blacklist}></textarea>
      <div class="row end"><button onclick={saveBl}>Сохранить список</button></div>
    </section>

    <section class="card block">
      <h3>Данные</h3>
      <div class="row">
        <button onclick={reimport}>Импортировать результаты скриптов</button>
        {#if confirmTrash}
          <span class="warn small">Удалить корзину навсегда? Это не отменить.</span>
          <button class="danger" onclick={emptyTrash}>Да, очистить</button>
          <button class="ghost" onclick={() => (confirmTrash = false)}>Нет</button>
        {:else}
          <button class="danger" onclick={() => (confirmTrash = true)}>Очистить корзину…</button>
        {/if}
      </div>
      {#if imported}
        <table>
          <tbody>
            {#each imported as r}
              <tr><td class="mono">{r.file.split("/").slice(-3).join("/")}</td>
                <td>{r.error ? r.error : `прочитано ${r.read}, новых ${r.added}, обновлено ${r.updated}, дублей в файле ${r.dupes}`}</td></tr>
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
  .block { padding: 14px 16px; margin-bottom: 12px; display: flex; flex-direction: column; gap: 9px; max-width: 900px; }
  h3 { margin: 0; font-size: 13px; }
  p { margin: 0; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .row.end { justify-content: flex-end; }
  .grow { flex: 1; min-width: 240px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
  label.inline { flex-direction: row; align-items: center; gap: 7px; color: var(--text); font-size: 13px; }
  .env { display: grid; gap: 6px; }
  .env-row { display: flex; gap: 10px; align-items: center; }
  .env-row .mono { min-width: 150px; }
  .versions { display: flex; gap: 20px; flex-wrap: wrap; }
  .err { color: var(--red); }
  .warn { color: var(--amber); }
  table { border-collapse: collapse; font-size: 12px; }
  td { padding: 4px 12px 4px 0; }
</style>
